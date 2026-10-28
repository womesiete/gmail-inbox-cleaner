"""Durable pipeline integration tests use real SQLite and recording transports."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from gmail_clean.domain import (
	Assessment, Config, Message, Window, content_excluded, eligible, epoch_ms,
	resolve_window,
)
from gmail_clean.gmail import GmailError, HistoryExpired, PageTokenExpired
from gmail_clean.jobs import JobRunner
from gmail_clean.runtime import identifier, insert, stamp
from gmail_clean.storage import Store


ANCHOR = datetime(2026, 10, 28, 12, tzinfo=UTC)
ACCOUNT = "pipeline@example.invalid"


def mail(mid="old", *, age_days=90, thread=None, labels=None, body="Complete. No reply required."):
	return Message(mid, thread or mid, epoch_ms(ANCHOR - timedelta(days=age_days)), set(labels or {"INBOX", "UNREAD"}), {"from": "Records <records@example.invalid>", "subject": "Completed account record"}, body=body, account_id=ACCOUNT)


class RecordingMailbox:
	def __init__(self, messages=()):
		self.messages = {message.id: message for message in messages}
		self.body_fetches = []
		self.metadata_fetches = []
		self.queries = []
		self.events = []
		self.history_requests = []
		self.thread_fetches = []
		self.expired_history = set()
		self.expire_page_once = False
		self.page_expired = False
		self.page_size = 500
		self.retrieval_errors = {}

	def profile(self):
		return {"emailAddress": ACCOUNT, "historyId": str(100 + len(self.events))}

	def identity(self):
		return ACCOUNT

	def labels(self):
		return [{"id": "INBOX", "name": "Inbox", "type": "system"}, {"id": "Label_Record", "name": "Records", "type": "user"}]

	def list_messages(self, query, page_token=None, page_size=500):
		self.queries.append((query, page_token))
		if self.expire_page_once and page_token and not self.page_expired:
			self.page_expired = True
			raise PageTokenExpired()
		after = re.search(r"after:(\d+)", query)
		before = re.search(r"before:(\d+)", query)
		ids = [message.id for message in self.messages.values() if "INBOX" in message.labels and not message.labels.intersection({"SPAM", "TRASH", "DRAFT"}) and (not after or message.internal_ms > int(after[1]) * 1000) and (not before or message.internal_ms < int(before[1]) * 1000)]
		offset = int(page_token or 0)
		size = min(page_size, self.page_size)
		batch = ids[offset:offset + size]
		# Duplicate IDs simulate Gmail's non-atomic enumeration and page replay.
		if offset and batch:
			batch = [ids[0], *batch]
		result = {"messages": [{"id": mid} for mid in batch]}
		if offset + size < len(ids):
			result["nextPageToken"] = str(offset + size)
		return result

	def get_messages(self, ids, body=False):
		result = {}
		for mid in ids:
			(self.body_fetches if body else self.metadata_fetches).append(mid)
			if mid in self.retrieval_errors:
				result[mid] = self.retrieval_errors[mid]
			elif mid not in self.messages:
				result[mid] = GmailError("missing", "Synthetic message disappeared")
			else:
				message = self.messages[mid]
				result[mid] = replace(message, labels=set(message.labels)) if body else replace(message, labels=set(message.labels), body="", missing_content=True)
		return result

	def thread_metadata(self, thread_id):
		self.thread_fetches.append(thread_id)
		return [replace(message, body="", missing_content=True) for message in self.messages.values() if message.thread_id == thread_id]

	def get_message(self, mid, body=False):
		result = self.get_messages([mid], body)[mid]
		if isinstance(result, Exception):
			raise result
		return result

	def history(self, start, page_token=None):
		self.history_requests.append((start, page_token))
		if start in self.expired_history:
			raise HistoryExpired()
		return {"history": self.events, "historyId": str(100 + len(self.events))}


class RecordingAI:
	def __init__(self):
		self.calls = []
		self.error = None
		self.score = 50
		self.action = "none"

	def assess(self, messages, labels, examples=(), **kwargs):
		self.calls.append({"ids": [message.id for message in messages], "examples": list(examples), **kwargs})
		if self.error:
			raise self.error
		return SimpleNamespace(assessments={message.id: Assessment(message.id, "financial", [], "high", self.action, "none", None, self.score, "low", False, "Explicit completion statement.") for message in messages}, errors={}, telemetry={})

	def close(self):
		self.error = None


class RecordingActions:
	def __init__(self, service):
		self.service = service
		self.planned = []

	def plan(self, job, message, action, labels, reason, approval, window, bypass=()):
		item = {"id": identifier(), "job_id": job, "message_id": message.id, "action": action, "labels": list(labels), "approval": approval, "window": window.to_dict(), "protection_bypass": list(bypass)}
		self.planned.append(item)
		insert(self.service.store, "actions", {"id": item["id"], "job_id": job, "message_id": message.id, "state": "proposed", "action": action, "approval": approval, "reason": reason})


class PipelineService:
	"""Dependency-injected application surface; no UI or network dependencies."""
	def __init__(self, path, messages, config=None):
		self.account_id = ACCOUNT
		self.store = Store(path, ACCOUNT)
		self.config = config or Config()
		self.now = ANCHOR
		self.clock = lambda: self.now
		self.mailbox = RecordingMailbox(messages)
		self.ai = RecordingAI()
		self.actions = RecordingActions(self)
		self._rules = []
		self._examples = []
		self.errors = []
		self.applies = []
		self.routes = []
		self.store.replace_labels([{"id": "Label_Record", "name": "Records", "type": "user"}])

	def eligible(self, message, window):
		return eligible(message, window) and eligible(message, resolve_window(self.config, self.now))

	def excluded(self, message):
		return content_excluded(message, self.config)

	def override(self, message):
		row = self.store.one("SELECT * FROM manual_overrides WHERE message_id=?", (message.id,))
		return {**row, **json.loads(row["payload_json"])} if row else None

	def rules(self):
		return self._rules

	def examples(self, message):
		return self._examples

	def ai_for(self, category=""):
		self.routes.append(category)
		return self.ai

	def context_messages(self, message, window, config=None):
		config = config or self.config
		return [self.mailbox.get_message(member.id, body=True) for member in self.mailbox.thread_metadata(message.thread_id) if member.id != message.id and window.contains(member.internal_ms) and (("SENT" in member.labels and config.sent_context_enabled) or ("INBOX" not in member.labels and "SENT" not in member.labels and config.archived_context_enabled)) and not content_excluded(member, config)]

	def reload_config(self):
		return self.config

	def heartbeat(self, job):
		self.store.execute("UPDATE jobs SET updated_at=? WHERE id=?", (stamp(self.now), job))

	def error(self, stage, exc, **kwargs):
		self.errors.append((stage, exc, kwargs))

	def apply_actions(self, **kwargs):
		self.applies.append(kwargs)


@pytest.fixture
def service(tmp_path):
	value = PipelineService(tmp_path / "jobs.sqlite3", [])
	yield value
	value.store.close()


def run_new(service):
	runner = JobRunner(service)
	job = runner.new()
	return runner, job, runner.run(job)


def test_full_job_enforces_scope_without_fetching_excluded_bodies(service):
	old = mail()
	recent = mail("recent", age_days=3)
	service.mailbox.messages = {item.id: item for item in (old, recent)}
	runner, job, summary = run_new(service)
	assert summary["state"] == "completed"
	assert service.mailbox.body_fetches == ["old"]
	assert [mid for call in service.ai.calls for mid in call["ids"]] == ["old"]
	assert [item["message_id"] for item in service.actions.planned] == ["old"]
	assert service.store.one("SELECT count(*) n FROM work_items WHERE state!='completed'")["n"] == 0
	assert runner.checkpoint("boundary_latest") == Window.from_dict(json.loads(service.store.one("SELECT window_json FROM jobs WHERE id=?", (job,))["window_json"])).to_dict()["latest_exclusive"]


def test_recent_excluded_reply_stales_older_conclusions_without_fetching_either_body(service):
	old = mail(thread="conversation")
	recent = mail("recent", age_days=13.5, thread="conversation")
	service.mailbox.messages = {item.id: item for item in (old, recent)}
	_, _, summary = run_new(service)
	assert summary["state"] == "completed"
	assert not service.mailbox.body_fetches
	assert not service.ai.calls
	assert not service.actions.planned
	assert service.store.one("SELECT decision FROM messages WHERE id='old'")["decision"] == "review"


def test_protections_and_approved_rules_short_circuit_ai(service):
	protected = mail("starred", labels={"INBOX", "STARRED"})
	rule_target = mail("rule")
	service.mailbox.messages = {item.id: item for item in (protected, rule_target)}
	service._rules = [{"enabled": True, "conditions": {"sender": "records@example.invalid", "subject_contains": "Completed"}, "action": "archive", "source": "explicit", "label_ids": ["Label_Record"]}]
	_, _, summary = run_new(service)
	assert summary["state"] == "completed"
	assert not service.ai.calls
	assert not service.mailbox.body_fetches
	assert service.store.one("SELECT decision FROM messages WHERE id='starred'")["decision"] == "keep"
	assert service.actions.planned[0]["message_id"] == "rule"
	assert service.actions.planned[0]["labels"] == ["Label_Record"]


def test_quiet_message_crosses_examination_boundary_without_history_event(service):
	service.mailbox.messages = {"quiet": mail("quiet", age_days=12)}
	runner, _, first = run_new(service)
	assert first["state"] == "completed"
	assert not service.ai.calls
	service.now += timedelta(days=3)
	service.config.reconciliation_interval_hours = 24 * 365
	_, _, second = run_new(service)
	assert second["state"] == "completed"
	assert service.mailbox.events == []
	assert service.mailbox.body_fetches == ["quiet"]
	assert service.ai.calls[-1]["ids"] == ["quiet"]
	assert "after:" in service.mailbox.queries[-1][0]
	assert runner.checkpoint("history_id") == "100"


def test_age_policy_transition_reuses_valid_assessment_without_history(service):
	service.mailbox.messages = {"aging": mail("aging", age_days=40)}
	_, _, first = run_new(service)
	assert first["state"] == "completed"
	assert service.store.one("SELECT decision FROM messages WHERE id='aging'")["decision"] == "review"
	assert len(service.ai.calls) == 1
	service.now += timedelta(days=40)
	_, _, second = run_new(service)
	assert second["state"] == "completed"
	assert len(service.ai.calls) == 1
	assert service.store.one("SELECT decision FROM messages WHERE id='aging'")["decision"] == "archive"


def test_resume_preserves_frozen_anchor_and_manifest(service):
	service.mailbox.messages = {"old": mail()}
	runner = JobRunner(service)
	job = runner.new()
	first = runner.run(job, should_stop=lambda: True)
	assert first["state"] == "paused"
	stored = service.store.one("SELECT anchor_utc,window_json FROM jobs WHERE id=?", (job,))
	service.now += timedelta(days=8)
	second = runner.run(job)
	assert second["state"] == "completed"
	assert service.store.one("SELECT anchor_utc,window_json FROM jobs WHERE id=?", (job,)) == stored
	assert len(service.ai.calls) == 1


def test_duplicate_pages_and_expired_page_tokens_deduplicate_before_processing(service):
	service.mailbox.messages = {str(i): mail(str(i)) for i in range(5)}
	service.mailbox.page_size = 2
	service.mailbox.expire_page_once = True
	_, _, summary = run_new(service)
	assert summary["state"] == "completed"
	assert service.mailbox.page_expired
	assert sorted(service.mailbox.body_fetches) == [str(i) for i in range(5)]
	assert len(service.actions.planned) == 5
	assert service.store.one("SELECT count(*) n FROM work_items WHERE stage='metadata'")["n"] == 5


def test_expired_history_reconciles_and_removals_are_not_lost(service):
	service.mailbox.messages = {"old": mail()}
	runner, _, _ = run_new(service)
	service.mailbox.messages["old"].labels.discard("INBOX")
	service.mailbox.messages["new"] = mail("new")
	service.mailbox.expired_history.add("100")
	service.mailbox.events = [{"id": "101", "labelsRemoved": [{"message": {"id": "old"}, "labelIds": ["INBOX"]}]}]
	_, _, summary = run_new(service)
	assert summary["state"] == "completed"
	assert service.store.one("SELECT in_inbox FROM messages WHERE id='old'")["in_inbox"] == 0
	assert service.store.one("SELECT decision FROM messages WHERE id='new'")["decision"] == "archive"
	assert runner.checkpoint("history_id") == "101"


def test_body_queue_rechecks_tighter_scope_and_privacy_before_download(service):
	service.mailbox.messages = {"old": mail()}
	runner = JobRunner(service)
	job = runner.new()
	window = resolve_window(service.config, service.now)
	assert runner.discover(job, window, service.config, None, None)
	assert runner.process_stage(job, "metadata", window, service.config, None, None)
	assert service.store.one("SELECT state FROM work_items WHERE stage='body'")["state"] == "pending"
	service.config = replace(service.config, earliest_date="2026-10-01")
	assert runner.run(job)["state"] == "completed"
	assert not service.mailbox.body_fetches
	assert not service.ai.calls


def test_old_job_preserves_removed_protections_and_privacy(service):
	service.config = replace(service.config, excluded_domains=["example.invalid"])
	frozen = Config.from_dict(service.config.to_dict())
	service.mailbox.messages = {"old": mail()}
	runner = JobRunner(service)
	job = runner.new()
	service.config = replace(service.config, excluded_domains=[])
	assert runner.run(job)["state"] == "completed"
	assert not service.mailbox.body_fetches
	assert not service.ai.calls
	assert runner.effective_config(frozen).excluded_domains == ["example.invalid"]


def test_threshold_changes_recommend_from_cache_without_new_ai(service):
	service.config = replace(service.config, age_enabled=False)
	service.ai.score = 30
	service.mailbox.messages = {"old": mail()}
	_, _, _ = run_new(service)
	assert service.store.one("SELECT decision FROM messages WHERE id='old'")["decision"] == "review"
	service.config.archive_threshold = 35
	_, _, _ = run_new(service)
	assert len(service.ai.calls) == 1
	assert service.store.one("SELECT decision FROM messages WHERE id='old'")["decision"] == "archive"


def test_scope_setting_change_forces_full_enumeration_without_waiting_for_periodic_reconcile(service):
	service.config = replace(service.config, earliest_date="2026-09-01", reconciliation_interval_hours=365 * 24)
	service.mailbox.messages = {"old": mail()}
	_, _, _ = run_new(service)
	assert not service.ai.calls
	service.config.earliest_date = None
	_, _, _ = run_new(service)
	assert service.ai.calls[0]["ids"] == ["old"]
	assert "after:" not in service.mailbox.queries[-1][0]


def test_history_replay_never_counts_as_feedback(service):
	service.mailbox.messages = {"old": mail()}
	service.mailbox.events = [{"id": "101", "labelsAdded": [{"message": {"id": "old"}, "labelIds": ["UNREAD"]}], "messages": [{"id": "old"}]}]
	_, _, _ = run_new(service)
	_, _, _ = run_new(service)
	assert service.store.one("SELECT count(*) n FROM feedback")["n"] == 0
	assert service.store.one("SELECT count(*) n FROM examples")["n"] == 0
	assert len(service.ai.calls) == 1


def test_retry_wait_and_retry_exhaustion_preserve_per_item_progress(service):
	service.config.max_retries = 1
	service.mailbox.messages = {"old": mail(), "other": mail("other")}
	service.mailbox.retrieval_errors["old"] = GmailError("network", "Synthetic offline condition")
	runner, job, first = run_new(service)
	assert first["state"] == "waiting"
	assert first["retry"] == 1
	assert service.store.one("SELECT state FROM work_items WHERE message_id='other' AND stage='metadata'")["state"] == "completed"
	service.now += timedelta(minutes=10)
	second = runner.run(job)
	assert second["state"] == "completed"
	assert second["failed"] == 1
	assert [item["message_id"] for item in service.actions.planned] == ["other"]
	assert len(service.errors) == 2


def test_configuration_failure_pauses_without_retry_loop(service):
	class ConfigurationError(RuntimeError):
		configuration = True

	service.ai.error = ConfigurationError("No credentials")
	service.mailbox.messages = {"old": mail()}
	_, _, summary = run_new(service)
	assert summary["state"] == "paused"
	assert summary["failed"] == 0 and summary["pending"] == 1
	assert summary["retry"] == 0
	assert not service.actions.planned


def test_example_text_obeys_frozen_and_current_privacy_and_date_bounds(service):
	current = mail()
	recent = mail("recent", age_days=2)
	private = mail("private", labels={"INBOX", "Private"})
	valid = mail("reviewed", age_days=60)
	for item in (current, recent, private, valid):
		service.store.upsert_message(item)
	service.config.excluded_label_ids = ["Private"]
	service._examples = [{"message_id": item.id, "body": item.body, "category": "financial"} for item in (recent, private, valid)]
	runner = JobRunner(service)
	assert [item["message_id"] for item in runner.examples(current, service.config, resolve_window(service.config, ANCHOR))] == ["reviewed"]


def test_stopped_job_is_not_silently_relabeled_paused(service):
	runner = JobRunner(service)
	job = runner.new()
	service.store.execute("UPDATE jobs SET state='stopped' WHERE id=?", (job,))
	assert runner.held(job, lambda: True)
	assert service.store.one("SELECT state FROM jobs WHERE id=?", (job,))["state"] == "stopped"


def test_explicit_download_all_fetches_protected_targets_without_classifying_them(service):
	service.config.download_all_bodies = True
	service.mailbox.messages = {"starred": mail("starred", labels={"INBOX", "STARRED"}), "recent": mail("recent", age_days=3)}
	_, _, summary = run_new(service)
	assert summary["state"] == "completed"
	assert service.mailbox.body_fetches == ["starred"]
	assert not service.ai.calls
	assert service.store.get_message("starred").body
	assert service.store.one("SELECT decision FROM messages WHERE id='starred'")["decision"] == "keep"


def test_label_only_rule_adds_labels_without_archiving_unresolved_work(service):
	service.ai.action = "reply_needed"
	service.mailbox.messages = {"old": mail()}
	service._rules = [{"enabled": True, "conditions": {"sender": "records@example.invalid"}, "action": "label", "source": "explicit", "label_ids": ["Label_Record"]}]
	_, _, summary = run_new(service)
	assert summary["state"] == "completed"
	assert service.store.one("SELECT decision FROM messages WHERE id='old'")["decision"] == "review"
	assert service.actions.planned
	assert all(item["action"] == "label" and item["labels"] == ["Label_Record"] for item in service.actions.planned)


def test_optional_model_routes_split_batches_and_default_for_unknown_category(service):
	service.config.ai_batch_size = 3
	service.config.optional_model_routes = {"promotion": "light-explicit", "newsletter": "news-explicit"}
	promotion = mail("promotion", labels={"INBOX", "CATEGORY_PROMOTIONS"})
	newsletter = mail("newsletter")
	newsletter.headers["list-id"] = "announcements.example.invalid"
	unknown = mail("unknown")
	service.mailbox.messages = {item.id: item for item in (promotion, newsletter, unknown)}
	_, _, summary = run_new(service)
	assert summary["state"] == "completed"
	assert set(service.routes) == {"promotion", "newsletter", ""}
	assert sorted(call["ids"] for call in service.ai.calls) == [["newsletter"], ["promotion"], ["unknown"]]


def test_optional_sent_context_is_read_only_and_never_an_action_target(service):
	service.config.sent_context_enabled = True
	target = mail("target", thread="conversation")
	sent = mail("sent", age_days=80, thread="conversation", labels={"SENT"})
	recent = mail("recent", age_days=1, thread="other", labels={"SENT"})
	service.mailbox.messages = {item.id: item for item in (target, sent, recent)}
	_, _, summary = run_new(service)
	assert summary["state"] == "completed"
	assert service.ai.calls[0]["ids"] == ["target"]
	assert [item.id for item in service.ai.calls[0]["context_messages"]] == ["sent"]
	assert "recent" not in service.mailbox.body_fetches
	assert {item["message_id"] for item in service.actions.planned} == {"target"}


def test_public_app_service_runs_real_ledger_and_replans_without_an_ai_call(tmp_path):
	from gmail_clean.service import AppService

	transport = RecordingMailbox([mail()])
	clients = []
	def factory(location, **kwargs):
		client = RecordingAI()
		clients.append(client)
		return client

	store = Store(tmp_path / "app.sqlite3", ACCOUNT)
	app = AppService(store=store, mailbox=transport, config=Config(age_enabled=False), data_dir=tmp_path / "app", clock=lambda: ANCHOR, ai_factory=factory)
	try:
		job = app.new_job()
		assert app.run_job(job)["state"] == "completed"
		assert len(clients[0].calls) == 1
		assert not store.query("SELECT * FROM actions")
		app.update_config(replace(app.config, archive_threshold=55))
		assert app.replan()["ai_calls"] == 0
		assert len(clients[0].calls) == 1
		assert store.one("SELECT action,state FROM actions") == {"action": "archive", "state": "proposed"}
	finally:
		app.close()


def test_control_signal_preserves_requested_stop_state_on_worker_connection(service):
	runner = JobRunner(service)
	job = runner.new()
	assert runner.run(job, should_stop=lambda: "stopped")["state"] == "stopped"
	assert service.store.one("SELECT state FROM jobs WHERE id=?", (job,))["state"] == "stopped"


def test_quiet_incremental_cycle_does_not_refetch_decided_inbox(service):
	service.config.reconciliation_interval_hours = 365 * 24
	service.mailbox.messages = {"old": mail()}
	_, _, first = run_new(service)
	assert first["state"] == "completed"
	assert len(service.ai.calls) == 1
	service.mailbox.metadata_fetches.clear()
	service.mailbox.thread_fetches.clear()
	service.now += timedelta(hours=1)
	_, job, second = run_new(service)
	assert second["state"] == "completed"
	assert not service.mailbox.metadata_fetches
	assert not service.mailbox.thread_fetches
	assert not service.store.query("SELECT * FROM work_items WHERE job_id=?", (job,))
	assert len(service.ai.calls) == 1


def test_metadata_stage_fetches_shared_thread_once_per_retrieval_batch(service):
	service.mailbox.messages = {mid: mail(mid, thread="conversation") for mid in ("first", "second")}
	runner = JobRunner(service)
	job = runner.new()
	window = resolve_window(service.config, service.now)
	assert runner.discover(job, window, service.config, None, None)
	assert runner.process_stage(job, "metadata", window, service.config, None, None)
	assert service.mailbox.thread_fetches == ["conversation"]


def test_incremental_age_crossing_revisits_only_newly_old_review(service):
	service.config.reconciliation_interval_hours = 365 * 24
	service.mailbox.messages = {"aging": mail("aging", age_days=40), "decided": mail("decided", age_days=100)}
	_, _, first = run_new(service)
	assert first["state"] == "completed"
	initial_calls = len(service.ai.calls)
	service.mailbox.metadata_fetches.clear()
	service.now += timedelta(days=40)
	_, _, second = run_new(service)
	assert second["state"] == "completed"
	assert set(service.mailbox.metadata_fetches) == {"aging"}
	assert len(service.ai.calls) == initial_calls
	assert service.store.one("SELECT decision FROM messages WHERE id='aging'")["decision"] == "archive"
	service.mailbox.metadata_fetches.clear()
	service.now += timedelta(hours=1)
	_, _, third = run_new(service)
	assert third["state"] == "completed"
	assert not service.mailbox.metadata_fetches


def test_deadline_requeues_when_due_without_history_but_not_before(service):
	service.config.reconciliation_interval_hours = 365 * 24
	service.config.age_enabled = False
	service.mailbox.messages = {"old": mail()}
	_, _, _ = run_new(service)
	due = service.now + timedelta(hours=2)
	service.store.execute("UPDATE messages SET deadline_utc=? WHERE id='old'", (stamp(due),))
	service.mailbox.metadata_fetches.clear()
	service.now += timedelta(hours=1)
	_, _, before = run_new(service)
	assert before["state"] == "completed"
	assert not service.mailbox.metadata_fetches
	service.now += timedelta(hours=2)
	_, _, after = run_new(service)
	assert after["state"] == "completed"
	assert set(service.mailbox.metadata_fetches) == {"old"}
	assert len(service.ai.calls) == 1


def test_invalidated_prediction_requeues_only_affected_local_message(service):
	service.config.reconciliation_interval_hours = 365 * 24
	service.mailbox.messages = {mid: mail(mid) for mid in ("affected", "unrelated")}
	_, _, _ = run_new(service)
	service.store.execute("UPDATE predictions SET valid=0 WHERE message_id='affected'")
	service.mailbox.metadata_fetches.clear()
	service.now += timedelta(hours=1)
	_, _, result = run_new(service)
	assert result["state"] == "completed"
	assert set(service.mailbox.metadata_fetches) == {"affected"}
	assert len(service.ai.calls) == 3


def test_rule_support_changes_do_not_requeue_or_invalidate_ai_cache(service):
	service.config.reconciliation_interval_hours = 365 * 24
	service.mailbox.messages = {"old": mail()}
	service._rules = [{"id": "unrelated-rule", "enabled": True, "conditions": {"sender": "other@example.invalid"}, "action": "keep", "support": 3, "counterexamples": 0, "updated_at": "old", "revision": 1}]
	_, _, _ = run_new(service)
	service._rules[0].update(support=5, counterexamples=1, updated_at="new")
	service.now += timedelta(hours=1)
	service.mailbox.metadata_fetches.clear()
	_, _, result = run_new(service)
	assert result["state"] == "completed"
	assert not service.mailbox.metadata_fetches
	assert len(service.ai.calls) == 1
	# Even a deliberate new full reconciliation reuses the same assessment.
	service.store.set_checkpoint("full_reconciliation", None)
	_, _, result = run_new(service)
	assert result["state"] == "completed"
	assert len(service.ai.calls) == 1


def test_semantic_rule_change_reconsiders_local_items_without_new_ai(service):
	service.config.reconciliation_interval_hours = 365 * 24
	service.mailbox.messages = {"old": mail()}
	_, _, _ = run_new(service)
	service._rules = [{"id": "new-rule", "enabled": True, "conditions": {"sender": "records@example.invalid"}, "action": "keep", "source": "explicit", "revision": 1}]
	service.now += timedelta(hours=1)
	service.mailbox.metadata_fetches.clear()
	_, _, result = run_new(service)
	assert result["state"] == "completed"
	assert service.mailbox.metadata_fetches == ["old"]
	assert service.store.one("SELECT decision FROM messages WHERE id='old'")["decision"] == "keep"
	assert len(service.ai.calls) == 1


def test_actual_prompt_version_invalidates_cached_assessment(service, monkeypatch):
	import gmail_clean.jobs as jobs
	service.config.reconciliation_interval_hours = 365 * 24
	service.mailbox.messages = {"old": mail()}
	_, _, _ = run_new(service)
	service.now += timedelta(hours=1)
	monkeypatch.setattr(jobs, "PROMPT_VERSION", "test-new-meaningful-prompt")
	_, _, result = run_new(service)
	assert result["state"] == "completed"
	assert len(service.ai.calls) == 2
	assert service.store.one("SELECT prompt_version FROM predictions ORDER BY created_at DESC LIMIT 1")["prompt_version"] == "test-new-meaningful-prompt"


def test_example_statistics_reuse_assessment_but_changed_prompt_text_reassesses(service):
	service.config.reconciliation_interval_hours = 365 * 24
	service.mailbox.messages = {"old": mail()}
	service.store.upsert_message(mail("reviewed", age_days=70, labels={"SENT"}))
	service._examples = [{"id": "e1", "message_id": "reviewed", "action": "archive", "body": "Completed record", "subject": "Receipt", "weight": 1, "created_at": "old", "updated_at": "old"}]
	_, _, first = run_new(service)
	assert first["state"] == "completed"
	assert len(service.ai.calls) == 1
	service._examples[0].update(weight=3, created_at="new", updated_at="new")
	service.store.set_checkpoint("full_reconciliation", None)
	_, _, same_prompt = run_new(service)
	assert same_prompt["state"] == "completed"
	assert len(service.ai.calls) == 1
	service._examples[0]["body"] = "Please reply"
	service.store.set_checkpoint("full_reconciliation", None)
	_, _, changed_prompt = run_new(service)
	assert changed_prompt["state"] == "completed"
	assert len(service.ai.calls) == 2


def test_classification_save_failure_retries_only_unresolved_item(service, monkeypatch):
	service.config.ai_batch_size = 4
	service.mailbox.messages = {str(i): mail(str(i)) for i in range(4)}
	runner = JobRunner(service)
	job = runner.new()
	original = runner.save_recommendation
	def save(*args, **kwargs):
		if args[1].id == "2":
			raise OSError("Synthetic third-item persistence failure")
		return original(*args, **kwargs)
	monkeypatch.setattr(runner, "save_recommendation", save)
	runner.run(job)
	states = {row["message_id"]: row["state"] for row in service.store.query("SELECT message_id,state FROM work_items WHERE stage='classification'")}
	assert states == {"0": "completed", "1": "completed", "2": "retry", "3": "completed"}
	assert len(service.ai.calls) == 1


@pytest.mark.parametrize("kind", ["auth", "permission", "account", "quota", "rate_limit", "data", "network"])
def test_fail_policy_preserves_decisions_and_separates_job_blocks(service, kind):
	runner = JobRunner(service)
	job = runner.new()
	service.store.upsert_message(mail())
	service.store.execute("UPDATE messages SET decision='archive' WHERE id='old'")
	runner.queue(job, "old", "classification")
	row = service.store.one("SELECT * FROM work_items WHERE job_id=?", (job,))
	runner.fail(job, row, GmailError(kind, "Synthetic policy failure", retry_after=75), service.config)
	work = service.store.one("SELECT * FROM work_items WHERE id=?", (row["id"],))
	assert service.store.one("SELECT decision FROM messages WHERE id='old'")["decision"] == "archive"
	if kind in {"auth", "permission", "account", "quota"}:
		assert work["state"] == "pending" and work["attempts"] == 0
		assert runner.summary(job)["state"] == "paused"
		assert runner.payload(job)["pause"]["kind"] == kind
	elif kind == "rate_limit":
		assert work["state"] == "retry" and work["attempts"] == 0
		assert runner.summary(job)["state"] == "waiting"
		backoff = runner.payload(job)["backoff"]
		assert datetime.fromisoformat(backoff["due"]) >= service.now + timedelta(seconds=75)
		assert backoff["batch_size"] < service.config.retrieval_batch_size
	elif kind == "data":
		assert work["state"] == "failed" and work["attempts"] == 1
	else:
		assert work["state"] == "retry" and work["attempts"] == 1


@pytest.mark.parametrize('stage,commit_cap', [('metadata', 3), ('body', 4)])
def test_retrieval_batches_commit_once_and_never_hold_writer_during_mailbox_calls(tmp_path, monkeypatch, stage, commit_cap):
	messages = [mail(f'batched-{index}') for index in range(8)]
	value = PipelineService(tmp_path / f'{stage}.sqlite', messages)
	try:
		runner = JobRunner(value)
		job = runner.new()
		window = resolve_window(value.config, ANCHOR)
		for message in messages:
			if stage == 'body':
				runner.metadata(job, replace(message, body='', missing_content=True), window, value.config)
			else:
				runner.queue(job, message.id, stage)
		for name in ('get_messages', 'thread_metadata'):
			original = getattr(value.mailbox, name)
			def checked(*args, _original=original, **kwargs):
				assert not value.store.conn.in_transaction, 'mailbox call while SQLite writer is held'
				return _original(*args, **kwargs)
			monkeypatch.setattr(value.mailbox, name, checked)
		commits = []
		original_execute = value.store.execute
		def counted_execute(sql, params=()):
			verb = sql.strip().split()[0].upper()
			if verb in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'} and not value.store.conn.in_transaction:
				commits.append(sql)
			return original_execute(sql, params)
		monkeypatch.setattr(value.store, 'execute', counted_execute)
		value.store.conn.set_trace_callback(lambda sql: commits.append(sql) if sql == 'COMMIT' else None)
		assert runner.process_stage(job, stage, window, value.config, None, None)
		value.store.conn.set_trace_callback(None)
		assert len(commits) <= commit_cap, f'{len(commits)} commits for one {len(messages)}-message batch'
		assert value.store.one('SELECT COUNT(*) n FROM work_items WHERE job_id=? AND stage=? AND state=\'completed\'', (job, stage))['n'] == len(messages)
	finally:
		value.store.close()


def test_batched_metadata_caches_thread_errors_and_isolates_failed_rows(tmp_path, monkeypatch):
	messages = [mail('bad-one', thread='broken'), mail('bad-two', thread='broken'), mail('good')]
	value = PipelineService(tmp_path / 'thread-errors.sqlite', messages)
	try:
		runner = JobRunner(value)
		job = runner.new()
		window = resolve_window(value.config, ANCHOR)
		for message in messages:
			runner.queue(job, message.id)
		original = value.mailbox.thread_metadata
		calls = []
		def thread(thread_id):
			assert not value.store.conn.in_transaction
			calls.append(thread_id)
			if thread_id == 'broken':
				raise GmailError('network', 'Offline transient')
			return original(thread_id)
		monkeypatch.setattr(value.mailbox, 'thread_metadata', thread)
		assert not runner.process_stage(job, 'metadata', window, value.config, None, None)
		assert calls.count('broken') == 1
		states = {row['message_id']: row['state'] for row in value.store.query("SELECT message_id,state FROM work_items WHERE stage='metadata'")}
		assert states == {'bad-one': 'retry', 'bad-two': 'retry', 'good': 'completed'}
		assert value.store.get_message('bad-one') is None
		assert value.store.get_message('good')
	finally:
		value.store.close()


def test_local_transition_queue_uses_one_batch_transaction(tmp_path, monkeypatch):
	messages = [mail(f'local-{index}') for index in range(25)]
	value = PipelineService(tmp_path / 'local-transitions.sqlite', messages)
	try:
		value.store.upsert_messages(messages)
		runner = JobRunner(value)
		job = runner.new()
		commits = []
		original = value.store.execute
		def counted(sql, params=()):
			if sql.strip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE'} and not value.store.conn.in_transaction:
				commits.append(sql)
			return original(sql, params)
		monkeypatch.setattr(value.store, 'execute', counted)
		value.store.conn.set_trace_callback(lambda sql: commits.append(sql) if sql == 'COMMIT' else None)
		runner.queue_local_transitions(job, resolve_window(value.config, ANCHOR), value.config, full=True)
		assert len(commits) == 2
		assert value.store.one('SELECT COUNT(*) n FROM work_items')['n'] == 25
	finally:
		value.store.conn.set_trace_callback(None)
		value.store.close()
