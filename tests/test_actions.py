"""Exercise the real action ledger against a deterministic message-scoped transport."""
from copy import deepcopy
from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import threading
from types import SimpleNamespace

import pytest

from gmail_clean.actions import ActionExecutor
from gmail_clean.domain import Config, Message, context_hash, epoch_ms, resolve_window
from gmail_clean.learning import Learning
from gmail_clean.storage import Store

ANCHOR = datetime(2026, 10, 28, 15, 30, tzinfo=UTC)
ACCOUNT = "owner@example.invalid"


class MemoryMailbox:
	"""Fake live Gmail state, including finite history and lost write responses."""

	def __init__(self):
		self.account = ACCOUNT
		self.messages = {}
		self.catalog = [{"id": label, "name": label, "type": "system"} for label in ("INBOX", "SENT", "UNREAD", "STARRED", "IMPORTANT", "TRASH", "SPAM", "DRAFT")]
		self.catalog += [{"id": label, "name": label, "type": "user"} for label in ("Label_reference", "Label_added", "Label_other")]
		self.calls = []
		self.events = []
		self.history_id = 10
		self.lose_response = False
		self.expired_history = False

	def identity(self):
		return self.account

	def labels(self):
		return deepcopy(self.catalog)

	def profile(self):
		return {"emailAddress": self.account, "historyId": str(self.history_id)}

	def get_message(self, mid, body=False):
		result = deepcopy(self.messages[mid])
		if not body:
			result.body = None
		return result

	def thread_metadata(self, thread_id):
		return [self.get_message(mid) for mid, message in self.messages.items() if message.thread_id == thread_id]

	def history(self, start, page_token=None):
		if self.expired_history:
			raise RuntimeError("Expired history")
		return {"history": [deepcopy(event) for event in self.events if int(event["id"]) > int(start)], "historyId": str(self.history_id)}

	def edit(self, mid, add=(), remove=()):
		message = self.messages[mid]
		added, removed = set(add) - message.labels, set(remove) & message.labels
		message.labels |= set(add)
		message.labels -= set(remove)
		self.history_id += 1
		event = {"id": str(self.history_id)}
		if added:
			event["labelsAdded"] = [{"message": {"id": mid, "threadId": message.thread_id}, "labelIds": sorted(added)}]
		if removed:
			event["labelsRemoved"] = [{"message": {"id": mid, "threadId": message.thread_id}, "labelIds": sorted(removed)}]
		self.events.append(event)

	def modify(self, ids, add, remove):
		# This is the only mutator exposed to the production ActionExecutor.
		self.calls.append((list(ids), list(add), list(remove)))
		for mid in ids:
			self.edit(mid, add, remove)
		if self.lose_response:
			self.lose_response = False
			raise OSError("Synthetic response lost after write committed")


@pytest.fixture
def app(tmp_path):
	store = Store(tmp_path / "mailbox.sqlite3", ACCOUNT)
	mailbox = MemoryMailbox()
	config = Config(dry_run=False, protect_starred=False)
	service = SimpleNamespace(store=store, mailbox=mailbox, config=config, account_id=ACCOUNT)
	service.reload_config = lambda: None
	service.refresh_labels = lambda: store.replace_labels(mailbox.labels())
	service.eligible = lambda message, window: "INBOX" in message.labels and not bool(message.labels & {"SPAM", "TRASH", "DRAFT"}) and window.contains(message.internal_ms) and resolve_window(service.config, window.anchor_utc).contains(message.internal_ms)
	service.overrides = {}
	service.override = lambda message: service.overrides.get(message.id)
	service.errors = []
	service.error = lambda stage, exc, **details: service.errors.append((stage, type(exc).__name__, details))
	service.refresh_labels()
	service.actions = ActionExecutor(service)
	service.window = resolve_window(config, ANCHOR)
	store.set_checkpoint("history_id", mailbox.profile()["historyId"])
	yield service
	store.close()


def seed(app, mid="old", *, labels=None, age_days=100, thread_id=None):
	message = Message(mid, thread_id or "thread-" + mid, epoch_ms(ANCHOR - timedelta(days=age_days)), labels=set(labels or {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "Label_reference"}), headers={"From": "sender@example.invalid", "Subject": "Completed record"}, body="The work is complete.", missing_content=False, account_id=ACCOUNT)
	app.mailbox.messages[mid] = deepcopy(message)
	message.context_version = context_hash(app.mailbox.thread_metadata(message.thread_id), app.config.sent_context_enabled)
	app.store.upsert_message(message)
	return message


def plan(app, message, labels=(), approval="automatic", bypass=(), action="archive"):
	return app.actions.plan("job-one", message, action, list(labels), "Reviewed completed correspondence", approval, app.window, bypass=bypass)


def state(app, aid):
	return app.store.one("SELECT * FROM actions WHERE id=?", (aid,))


def test_archive_uses_only_minimal_label_deltas_and_preserves_live_state(app):
	message = seed(app)
	aid = plan(app, message, ["Label_reference", "Label_added"])
	# New unrelated state between planning and execution must survive.
	app.mailbox.edit(message.id, add=["Label_other"], remove=["UNREAD"])
	result = app.actions.apply([aid])
	assert result["applied"] == 1
	assert app.mailbox.calls == [(["old"], ["Label_added"], ["INBOX"])]
	assert app.mailbox.messages["old"].labels == {"STARRED", "IMPORTANT", "Label_reference", "Label_added", "Label_other"}
	assert state(app, aid)["state"] == "applied"
	assert app.store.one("SELECT count(*) AS n FROM attempts WHERE action_id=? AND state='applied'", (aid,))["n"] == 1


def test_live_dry_run_has_no_write_and_records_queued_proposal(app):
	app.config.dry_run = True
	aid = plan(app, seed(app))
	assert state(app, aid)["state"] == "proposed"
	assert app.actions.apply([aid])["dry_run"]
	assert not app.mailbox.calls
	assert "INBOX" in app.mailbox.messages["old"].labels


def test_reload_enabling_simulation_prevents_write(app):
	message = seed(app)
	aid = plan(app, message)
	app.reload_config = lambda: setattr(app.config, "dry_run", True)
	result = app.actions.apply([aid])
	assert not app.mailbox.calls
	assert result.get("dry_run")


def test_newer_reply_invalidates_forward_action_without_touching_reply(app):
	message = seed(app)
	aid = plan(app, message)
	seed(app, "new-reply", age_days=1, thread_id=message.thread_id)
	app.actions.apply([aid])
	assert state(app, aid)["state"] == "stale"
	assert "Conversation" in state(app, aid)["reason"]
	assert not app.mailbox.calls
	assert "INBOX" in app.mailbox.messages["new-reply"].labels


def test_tightened_current_scope_never_expands_frozen_job(app):
	message = seed(app)
	aid = plan(app, message)
	app.config.earliest_date = "2026-10-01"
	app.actions.apply([aid])
	assert state(app, aid)["state"] == "stale"
	assert not app.mailbox.calls
	outside = seed(app, "outside", age_days=1)
	with pytest.raises(ValueError, match="scope"):
		plan(app, outside)


def test_new_protection_and_current_manual_keep_invalidate_action(app):
	app.config.protect_starred = True
	message = seed(app, labels={"INBOX", "Label_reference"})
	aid = plan(app, message)
	app.mailbox.edit(message.id, add=["STARRED"])
	app.actions.apply([aid])
	assert state(app, aid)["state"] == "stale"
	assert "protection" in state(app, aid)["reason"]
	assert not app.mailbox.calls
	message2 = seed(app, "second", labels={"INBOX"})
	other = plan(app, message2)
	app.overrides[message2.id] = {"action": "keep", "context_version": message2.context_version}
	app.actions.apply([other])
	assert state(app, other)["state"] == "stale"
	assert not app.mailbox.calls


def test_manual_bypass_applies_only_to_displayed_protections(app):
	app.config.protect_starred = True
	message = seed(app)
	aid = plan(app, message, approval="manual", bypass={"starred"})
	assert app.actions.apply([aid])["applied"] == 1
	assert "STARRED" in app.mailbox.messages[message.id].labels
	message2 = seed(app, "second")
	other = plan(app, message2, approval="manual", bypass={"starred"})
	app.config.protected_label_ids = ["Label_reference"]
	app.actions.apply([other])
	assert state(app, other)["state"] == "stale"
	assert len(app.mailbox.calls) == 1


def test_changed_policy_stales_pending_actions_and_deleted_labels_conflict(app):
	message = seed(app)
	aid = plan(app, message)
	app.config.archive_threshold = 20
	app.actions.apply([aid])
	assert state(app, aid)["state"] == "stale"
	second = seed(app, "second")
	other = plan(app, second, ["Label_added"])
	app.mailbox.catalog = [label for label in app.mailbox.catalog if label["id"] != "Label_added"]
	app.actions.apply([other])
	assert state(app, other)["state"] == "conflict"
	assert not app.mailbox.calls


def test_wrong_account_never_mutates_mailbox(app):
	aid = plan(app, seed(app))
	app.mailbox.account = "different@example.invalid"
	with pytest.raises(ValueError, match="account"):
		app.actions.apply([aid])
	assert not app.mailbox.calls


def test_lost_response_after_success_is_reconciled_without_duplicate_write(app):
	aid = plan(app, seed(app), ["Label_added"])
	app.mailbox.lose_response = True
	app.actions.apply([aid])
	assert state(app, aid)["state"] == "uncertain"
	assert "INBOX" not in app.mailbox.messages["old"].labels
	assert len(app.mailbox.calls) == 1
	app.actions.apply([aid])
	assert state(app, aid)["state"] == "reconciled"
	assert len(app.mailbox.calls) == 1
	# A readback establishes effects, not who owns them after a lost response.
	app.actions.undo([aid])
	assert state(app, aid)["state"] == "conflict"
	assert "ownership" in state(app, aid)["reason"]
	assert len(app.mailbox.calls) == 1


def test_lost_response_with_absent_effects_requires_explicit_retry(app):
	aid = plan(app, seed(app), ["Label_added"])
	app.actions.mark(aid, "uncertain")
	app.actions.apply([aid])
	assert state(app, aid)["state"] == "retry"
	assert not app.mailbox.calls
	assert app.actions.apply([aid])["applied"] == 1


def test_partial_effects_after_unknown_write_are_visible_conflicts(app):
	aid = plan(app, seed(app), ["Label_added"])
	app.actions.mark(aid, "uncertain")
	app.mailbox.edit("old", add=["Label_added"])
	app.actions.apply([aid])
	assert state(app, aid)["state"] == "conflict"
	assert not app.mailbox.calls


def test_already_satisfied_effects_claim_no_new_app_ownership(app):
	aid = plan(app, seed(app), ["Label_added"])
	app.mailbox.edit("old", add=["Label_added"], remove=["INBOX"])
	app.actions.apply([aid])
	assert state(app, aid)["state"] == "satisfied"
	assert not app.mailbox.calls
	assert not app.store.one("SELECT id FROM attempts WHERE action_id=?", (aid,))
	app.actions.undo([aid])
	assert not app.mailbox.calls


def test_undo_cancels_unexecuted_queue_without_a_gmail_change(app):
	aid = plan(app, seed(app))
	app.actions.undo([aid])
	assert state(app, aid)["state"] == "cancelled"
	assert not app.mailbox.calls


def test_undo_outside_narrowed_window_restores_only_original_owned_deltas(app):
	message = seed(app)
	aid = plan(app, message, ["Label_added", "Label_reference"])
	app.actions.apply([aid])
	app.mailbox.edit("old", add=["Label_other"], remove=["UNREAD"])
	seed(app, "new-reply", age_days=1, thread_id=message.thread_id)
	app.config.earliest_date = "2026-10-01"
	result = app.actions.undo([aid])
	assert result["applied"] == 1
	assert state(app, aid)["state"] == "undone"
	assert app.mailbox.messages["old"].labels == {"INBOX", "STARRED", "IMPORTANT", "Label_reference", "Label_other"}
	assert "INBOX" in app.mailbox.messages["new-reply"].labels
	assert app.mailbox.calls[-1] == (["old"], ["INBOX"], ["Label_added"])


def test_undo_rejects_ambiguous_relevant_edits_even_when_current_state_matches(app):
	aid = plan(app, seed(app), ["Label_added"])
	app.actions.apply([aid])
	app.mailbox.edit("old", remove=["Label_added"])
	app.mailbox.edit("old", add=["Label_added"])
	app.actions.undo([aid])
	inverse = app.store.one("SELECT * FROM actions WHERE undo_of=?", (aid,))
	assert inverse["state"] == "conflict"
	assert "Intervening" in inverse["reason"]
	assert len(app.mailbox.calls) == 1


@pytest.mark.parametrize("case", ["deleted", "protected", "meaning", "history_expired"])
def test_undo_cannot_remove_deleted_protected_or_ambiguous_label(app, case):
	aid = plan(app, seed(app), ["Label_added"])
	app.actions.apply([aid])
	if case == "deleted":
		app.mailbox.catalog = [label for label in app.mailbox.catalog if label["id"] != "Label_added"]
	elif case == "protected":
		app.config.protected_label_ids = ["Label_added"]
	elif case == "meaning":
		app.config.label_meanings["Label_added"] = "requires_attention"
	else:
		app.mailbox.expired_history = True
	app.actions.undo([aid])
	inverse = app.store.one("SELECT * FROM actions WHERE undo_of=?", (aid,))
	assert inverse["state"] == "conflict"
	assert len(app.mailbox.calls) == 1


def test_identical_deltas_group_into_bounded_message_batches(app):
	app.config.execution_batch_size = 2
	ids = [plan(app, seed(app, f"message-{n}"), ["Label_added"]) for n in range(5)]
	result = app.actions.apply(ids)
	assert result["applied"] == 5
	assert len(app.mailbox.calls) == 3
	assert sorted(len(call[0]) for call in app.mailbox.calls) == [1, 2, 2]
	assert {mid for call in app.mailbox.calls for mid in call[0]} == {f"message-{n}" for n in range(5)}


def test_label_only_action_never_removes_inbox_or_existing_labels(app):
	aid = plan(app, seed(app), ["Label_added"], action="label")
	app.actions.apply([aid])
	assert app.mailbox.calls == [(["old"], ["Label_added"], [])]
	assert "INBOX" in app.mailbox.messages["old"].labels


def test_missing_live_message_isolated_as_conflict(app):
	aid = plan(app, seed(app))
	del app.mailbox.messages["old"]
	app.actions.apply([aid])
	assert state(app, aid)["state"] == "conflict"
	assert not app.mailbox.calls


def test_plan_rejects_arbitrary_actions_and_unknown_label_ids(app):
	message = seed(app)
	with pytest.raises(ValueError, match="action"):
		plan(app, message, action="delete")
	with pytest.raises(ValueError, match="label"):
		plan(app, message, ["Invented_Label"])
	assert not app.mailbox.calls


def test_simulation_enabled_mid_chunk_holds_all_already_ready_items(app):
	ids = [plan(app, seed(app, f"item-{n}")) for n in range(2)]
	calls = 0
	def reload_config():
		nonlocal calls
		calls += 1
		# Initial reload, both first preflights, first final preflight finish live.
		if calls == 5:
			app.config.dry_run = True
	app.reload_config = reload_config
	result = app.actions.apply(ids)
	assert not app.mailbox.calls
	assert result["dry_run"]
	assert all("INBOX" in item.labels for item in app.mailbox.messages.values())
	assert all(state(app, aid)["state"] == "proposed" for aid in ids)


def test_policy_changed_mid_chunk_invalidates_every_old_ready_item(app):
	ids = [plan(app, seed(app, f"item-{n}")) for n in range(2)]
	calls = 0
	def reload_config():
		nonlocal calls
		calls += 1
		if calls == 5:
			app.config.protected_domains = ["example.invalid"]
	app.reload_config = reload_config
	app.actions.apply(ids)
	assert not app.mailbox.calls
	assert all(state(app, aid)["state"] == "stale" for aid in ids)


@pytest.mark.parametrize("committed", [True, False])
def test_process_crash_preserves_sending_attempt_and_recovers_from_reopened_db(app, committed):
	class SimulatedCrash(BaseException):
		"""Bypass normal error handling to model abrupt process death."""

	aid = plan(app, seed(app), ["Label_added"])
	modify = app.mailbox.modify
	def crashing_modify(ids, add, remove):
		if committed:
			modify(ids, add, remove)
		raise SimulatedCrash()
	app.mailbox.modify = crashing_modify
	with pytest.raises(SimulatedCrash):
		app.actions.apply([aid])
	assert state(app, aid)["state"] == "executing"
	assert app.store.one("SELECT state FROM attempts WHERE action_id=?", (aid,))["state"] == "sending"
	# A fresh connection reads the committed intent even though no receipt exists.
	previous_store = app.store
	reopened = Store(previous_store.path, ACCOUNT)
	app.store = reopened
	app.actions = ActionExecutor(app)
	app.mailbox.modify = modify
	try:
		app.actions.apply([aid])
		assert state(app, aid)["state"] == ("reconciled" if committed else "retry")
		assert len(app.mailbox.calls) == (1 if committed else 0)
		if not committed:
			assert app.actions.apply([aid])["applied"] == 1
			assert len(app.mailbox.calls) == 1
	finally:
		reopened.close()
		app.store = previous_store


def test_stale_undo_preview_can_be_recreated_after_enabling_live_execution(app):
	aid = plan(app, seed(app), ["Label_added"])
	app.actions.apply([aid])
	app.config.dry_run = True
	app.actions.undo([aid])
	preview = app.store.one("SELECT id FROM actions WHERE undo_of=?", (aid,))["id"]
	app.actions.mark(preview, "stale", "Settings changed while undo was in simulation")
	app.config.dry_run = False
	result = app.actions.undo([aid])
	assert result["applied"] == 1
	assert state(app, preview)["state"] == "cancelled"
	assert state(app, aid)["state"] == "undone"
	assert len(app.mailbox.calls) == 2


@pytest.mark.parametrize("evidence", ["unavailable", "missing", "inverse"])
def test_receipt_without_unique_expected_history_keeps_effect_but_blocks_undo(app, evidence):
	aid = plan(app, seed(app), ["Label_added"])
	original_history = app.mailbox.history
	if evidence == "unavailable":
		app.mailbox.expired_history = True
	elif evidence == "missing":
		app.mailbox.history = lambda start, page_token=None: {"history": [], "historyId": str(app.mailbox.history_id)}
	else:
		original_modify = app.mailbox.modify
		def modify_and_intervene(ids, add, remove):
			original_modify(ids, add, remove)
			app.mailbox.edit("old", remove=["Label_added"])
			app.mailbox.edit("old", add=["Label_added"])
		app.mailbox.modify = modify_and_intervene
	assert app.actions.apply([aid])["applied"] == 1
	assert state(app, aid)["state"] == "applied"
	app.mailbox.expired_history = False
	app.mailbox.history = original_history
	app.actions.undo([aid])
	assert state(app, aid)["state"] == "conflict"
	assert "Label_added" in app.mailbox.messages["old"].labels
	assert "INBOX" not in app.mailbox.messages["old"].labels
	assert len(app.mailbox.calls) == 1


def test_receipt_deduplicates_history_replays_without_losing_normal_undo(app):
	aid = plan(app, seed(app), ["Label_added"])
	original_history = app.mailbox.history
	def replayed_history(start, page_token=None):
		page = original_history(start, page_token)
		page["history"] += deepcopy(page["history"])
		return page
	app.mailbox.history = replayed_history
	assert app.actions.apply([aid])["applied"] == 1
	assert app.actions.undo([aid])["applied"] == 1
	assert state(app, aid)["state"] == "undone"


@pytest.mark.parametrize("pagination,expected_requests", [("cycle", 2), ("unbounded", 100)])
def test_undo_history_inspection_has_bounded_requests_and_rejects_token_cycles(app, pagination, expected_requests):
	aid = plan(app, seed(app), ["Label_added"])
	assert app.actions.apply([aid])["applied"] == 1
	requests = []
	def incomplete_history(start, page_token=None):
		requests.append(page_token)
		return {"history": [], "historyId": str(app.mailbox.history_id),
			"nextPageToken": "repeated-token" if pagination == "cycle" else str(len(requests))}
	app.mailbox.history = incomplete_history
	app.actions.undo([aid])
	inverse = app.store.one("SELECT * FROM actions WHERE undo_of=?", (aid,))
	assert inverse["state"] == "conflict"
	assert "cannot establish safe undo ownership" in inverse["reason"]
	assert len(requests) == expected_requests
	assert len(app.mailbox.calls) == 1
	assert "Label_added" in app.mailbox.messages["old"].labels


def test_thousand_actions_use_bulk_reads_thread_deduplication_and_shared_receipt_checkpoints(app):
	# Label additions leave thread membership unchanged, allowing a large single
	# conversation to exercise both preflight safety phases without stale targets.
	count = 1000
	messages = [Message(f"bulk-{n}", "shared-thread", epoch_ms(ANCHOR - timedelta(days=100)),
		labels={"INBOX", "UNREAD"}, headers={"From": "sender@example.invalid", "Subject": "Completed record"},
		body="Complete.", account_id=ACCOUNT) for n in range(count)]
	for message in messages:
		app.mailbox.messages[message.id] = deepcopy(message)
	context = context_hash(messages, False)
	ids = []
	with app.store.transaction():
		for message in messages:
			message.context_version = context
			app.store.upsert_message(message)
			ids.append(plan(app, message, ["Label_added"], action="label"))
	requests = Counter()
	def get_messages(message_ids, body=False):
		requests["bulk"] += 1
		return {mid: MemoryMailbox.get_message(app.mailbox, mid, body) for mid in message_ids}
	def get_message(mid, body=False):
		requests["single"] += 1
		return MemoryMailbox.get_message(app.mailbox, mid, body)
	def thread_metadata(thread_id):
		requests["thread"] += 1
		return [MemoryMailbox.get_message(app.mailbox, mid) for mid, message in app.mailbox.messages.items()
			if message.thread_id == thread_id]
	def profile():
		requests["profile"] += 1
		return MemoryMailbox.profile(app.mailbox)
	def history(start, page_token=None):
		requests["history"] += 1
		return MemoryMailbox.history(app.mailbox, start, page_token)
	app.mailbox.get_messages = get_messages
	app.mailbox.get_message = get_message
	app.mailbox.thread_metadata = thread_metadata
	app.mailbox.profile = profile
	app.mailbox.identity = lambda: app.mailbox.profile()["emailAddress"]
	app.mailbox.history = history
	assert app.actions.apply(ids)["applied"] == count
	assert requests["single"] == 0
	assert requests["bulk"] == 21  # Initial grouping, then preflight/readback per chunk.
	assert requests["thread"] == 11
	assert requests["profile"] == 32  # Identity + before/after checkpoints, not per item.
	assert requests["history"] == 10
	assert len(app.mailbox.calls) == 10
	checkpoints = Counter(json.loads(row["payload_json"])["receipt_history_id"]
		for row in app.store.query("SELECT payload_json FROM actions WHERE state='applied'"))
	assert len(checkpoints) == 10 and set(checkpoints.values()) == {100}
	assert not app.actions._prefetched_messages and not app.actions._prefetched_threads


def test_bulk_part_error_falls_back_per_message_without_touching_other_targets(app):
	ids = [plan(app, seed(app, mid), ["Label_added"]) for mid in ("valid", "missing")]
	del app.mailbox.messages["missing"]
	def get_messages(message_ids, body=False):
		return {mid: (MemoryMailbox.get_message(app.mailbox, mid, body) if mid in app.mailbox.messages else KeyError(mid))
			for mid in message_ids}
	app.mailbox.get_messages = get_messages
	assert app.actions.apply(ids)["applied"] == 1
	assert state(app, ids[0])["state"] == "applied"
	assert state(app, ids[1])["state"] == "conflict"
	assert app.mailbox.calls == [(["valid"], ["Label_added"], ["INBOX"])]


def test_cancellation_before_write_preserves_queue_and_clears_live_caches(app):
	ids = [plan(app, seed(app, f"item-{n}"), ["Label_added"]) for n in range(2)]
	stop = threading.Event()
	bulk_calls = 0
	def get_messages(message_ids, body=False):
		nonlocal bulk_calls
		bulk_calls += 1
		if bulk_calls == 2:  # The final safety preflight's metadata batch.
			stop.set()
		return {mid: MemoryMailbox.get_message(app.mailbox, mid, body) for mid in message_ids}
	app.mailbox.get_messages = get_messages
	assert app.actions.apply(ids, should_stop=stop.is_set)["stopped"]
	assert not app.mailbox.calls
	assert not app.store.one("SELECT id FROM attempts")
	assert all(state(app, aid)["state"] == "queued" for aid in ids)
	assert not app.actions._prefetched_messages and not app.actions._prefetched_threads
	stop.clear()
	assert app.actions.apply(ids)["applied"] == 2


def test_cancellation_after_write_records_unknown_receipt_then_resumes_without_duplicate(app):
	app.config.execution_batch_size = 1
	ids = [plan(app, seed(app, f"item-{n}"), ["Label_added"]) for n in range(2)]
	stop = threading.Event()
	original_modify = app.mailbox.modify
	def modify_and_stop(message_ids, add, remove):
		original_modify(message_ids, add, remove)
		stop.set()
	app.mailbox.modify = modify_and_stop
	assert app.actions.apply(ids, should_stop=stop.is_set)["stopped"]
	assert len(app.mailbox.calls) == 1
	assert sorted(state(app, aid)["state"] for aid in ids) == ["queued", "uncertain"]
	app.mailbox.modify = original_modify
	stop.clear()
	app.actions.apply(ids)
	assert len(app.mailbox.calls) == 2
	assert {mid for call in app.mailbox.calls for mid in call[0]} == {"item-0", "item-1"}
	assert sorted(state(app, aid)["state"] for aid in ids) == ["applied", "reconciled"]


def test_undo_history_cooperates_with_stop_and_can_resume_original_inverse(app):
	aid = plan(app, seed(app), ["Label_added"])
	assert app.actions.apply([aid])["applied"] == 1
	stop = threading.Event()
	requests = []
	original_history = app.mailbox.history
	def slow_history(start, page_token=None):
		requests.append(page_token)
		stop.set()
		return {"history": [], "historyId": str(app.mailbox.history_id), "nextPageToken": "more"}
	app.mailbox.history = slow_history
	assert app.actions.undo([aid], should_stop=stop.is_set)["stopped"]
	inverse = app.store.one("SELECT * FROM actions WHERE undo_of=?", (aid,))
	assert inverse["state"] == "queued"
	assert len(requests) == 1 and len(app.mailbox.calls) == 1
	stop.clear()
	app.mailbox.history = original_history
	assert app.actions.apply([inverse["id"]])["applied"] == 1
	assert state(app, aid)["state"] == "undone"


def test_account_change_after_cached_preflight_is_caught_by_prewrite_checkpoint(app):
	ids = [plan(app, seed(app, f"item-{n}")) for n in range(2)]
	original_preflight = app.actions.preflight
	checks = 0
	def preflight_and_switch(action):
		nonlocal checks
		result = original_preflight(action)
		checks += 1
		if checks == 4:
			app.mailbox.account = "different@example.invalid"
		return result
	app.actions.preflight = preflight_and_switch
	with pytest.raises(ValueError, match="account"):
		app.actions.apply(ids)
	assert not app.mailbox.calls
	assert not app.actions._prefetched_messages and not app.actions._prefetched_threads


@pytest.mark.parametrize("edit_during", ["preflight", "checkpoint"])
def test_enabled_keep_rule_edit_invalidates_already_loaded_automatic_archive(app, edit_during):
	ids = [plan(app, seed(app, f"item-{n}")) for n in range(2)]
	learning = Learning(app.store)
	def edit_rule():
		learning.save_rule({"conditions": {"sender": "sender@example.invalid", "subject_contains": "Completed"},
			"action": "keep", "enabled": True, "source": "explicit"})
	if edit_during == "preflight":
		reloads = 0
		def reload_config():
			nonlocal reloads
			reloads += 1
			if reloads == 5:
				edit_rule()
		app.reload_config = reload_config
	else:
		profile = app.mailbox.profile
		def profile_and_edit():
			edit_rule()
			return profile()
		app.mailbox.profile = profile_and_edit
	assert app.actions.apply(ids)["applied"] == 0
	assert all(state(app, aid)["state"] == "stale" for aid in ids)
	assert not app.mailbox.calls
	assert not app.store.one("SELECT id FROM attempts")
	assert all("INBOX" in item.labels for item in app.mailbox.messages.values())
