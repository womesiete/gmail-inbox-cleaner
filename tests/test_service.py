"""Integration checks for privacy, explicit review, workers, and connection state."""
from datetime import UTC, datetime, timedelta
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from gmail_clean.auth import AuthenticationError
from gmail_clean.demo import DEMO_ACCOUNT, DemoMailbox
from gmail_clean.domain import Config, Message, context_hash, epoch_ms, resolve_window
from gmail_clean.service import AppService

ANCHOR = datetime(2026, 10, 28, 12, tzinfo=UTC)


class FakeLive:
	"""A dependency-injected live transport; it performs no network operations."""
	is_demo = False

	def __init__(self, identity="first@example.invalid"):
		self.box = DemoMailbox(count=100, anchor=ANCHOR)
		self.account = identity

	def identity(self):
		return self.account

	def labels(self):
		return self.box.labels()

	def get_message(self, mid, body=False):
		message = self.box.get_message(mid, body)
		message.account_id = self.account
		return message

	def thread_metadata(self, thread_id):
		items = self.box.thread_metadata(thread_id)
		for item in items:
			item.account_id = self.account
		return items

	def profile(self):
		return {**self.box.profile(), "emailAddress": self.account}

	def modify(self, ids, add, remove):
		return self.box.modify(ids, add, remove)

	def history(self, start, page_token=None):
		return self.box.history(start, page_token)


def make_service(tmp_path, config=None, mailbox=None):
	mailbox = mailbox or DemoMailbox(count=100, anchor=ANCHOR)
	return AppService(mailbox=mailbox, config=config or Config(ai_module="gmail_clean.demo_ai"),
		data_dir=tmp_path, demo=isinstance(mailbox, DemoMailbox), clock=lambda: ANCHOR)


def cache(service, mid):
	message = service.mailbox.get_message(mid, body=True)
	message.context_version = context_hash(service.mailbox.thread_metadata(message.thread_id), service.config.sent_context_enabled)
	service.store.upsert_message(message)
	return message


def test_demo_rejects_injected_live_adapter_before_any_gmail_read(tmp_path):
	class Forbidden:
		def identity(self):
			raise AssertionError("real Gmail must never be accessed by demo")
	with pytest.raises(ValueError, match="offline DemoMailbox"):
		AppService(mailbox=Forbidden(), demo=True, data_dir=tmp_path)
	service = AppService(demo=True, count=50, data_dir=tmp_path)
	try:
		assert service.config.label_meanings["label_action"] == "requires_attention"
		with pytest.raises(ValueError):
			service.connect("credentials.json")
	finally:
		service.close()
		service.close()  # GUI teardown and CLI finally blocks can both call close.


def test_worker_connections_are_fresh_and_owned_by_the_calling_thread(tmp_path):
	service = AppService(demo=True, count=50, data_dir=tmp_path, clock=lambda: ANCHOR)
	try:
		main_store = service.store
		def operation():
			with service.worker() as worker:
				assert worker.store is not main_store
				# The demo adapter's synchronized API is shared; SQLite remains
				# strictly worker-owned and live Gmail clients are never shared.
				assert worker.mailbox is service._mailbox
				worker.store.set_checkpoint("worker-ran", True)
				return worker.account_id
		with ThreadPoolExecutor(max_workers=1) as pool:
			assert pool.submit(operation).result() == DEMO_ACCOUNT
		assert service.store.checkpoint("worker-ran") is True
	finally:
		service.close()


def test_disconnect_invalidates_already_created_worker_and_reconnect_epoch(tmp_path, monkeypatch):
	from gmail_clean.auth import AuthManager
	monkeypatch.setattr(AuthManager, "disconnect", lambda self, revoke=False: None)
	service = make_service(tmp_path, mailbox=FakeLive())
	service._write_identity()
	try:
		with service.worker() as worker:
			assert worker.mailbox.identity() == service.account_id
			service.disconnect()
			with pytest.raises(AuthenticationError, match="disconnected or changed"):
				_ = worker.mailbox
			with pytest.raises(AuthenticationError):
				_ = service.mailbox
			# Reconnecting even to the same account cannot revive a cached old token.
			service._write_identity(connected=True)
			with pytest.raises(AuthenticationError):
				_ = worker.mailbox
	finally:
		service.close()


def test_explicit_account_switch_uses_separate_database_and_keeps_old_ledger(tmp_path, monkeypatch):
	from gmail_clean.auth import AuthManager
	service = make_service(tmp_path, mailbox=FakeLive())
	first_store_path = service.store.path
	service.store.set_checkpoint("old-account-evidence", {"kept": True})
	service.config.label_meanings = {"label_records": "requires_attention"}
	second = FakeLive("second@example.invalid")
	monkeypatch.setattr(AuthManager, "connect", lambda self, path: second)
	try:
		result = service.connect("not-read-by-test.json")
		assert result["account_id"] == "second@example.invalid"
		assert service.store.path != first_store_path
		assert service.store.checkpoint("old-account-evidence") is None
		assert service.config.label_meanings == {}
		from gmail_clean.storage import Store
		with Store(first_store_path, "first@example.invalid") as previous:
			assert previous.checkpoint("old-account-evidence") == {"kept": True}
	finally:
		service.close()


def test_body_download_respects_window_privacy_and_size_limit(tmp_path):
	config = Config(ai_module="gmail_clean.demo_ai", body_limit=128)
	service = make_service(tmp_path, config=config)
	try:
		allowed = service.mailbox.messages[f"d{1:015x}"]
		excluded = service.mailbox.messages[f"d{2:015x}"]
		recent = service.mailbox.messages[f"d{31:015x}"]
		allowed.body = "long text " * 100
		config.excluded_senders = [excluded.sender]
		service.update_config(config)
		service.mailbox.body_reads.clear()
		assert service.download_bodies([allowed.id, excluded.id, recent.id]) == {"downloaded": 1}
		assert service.mailbox.body_reads == [allowed.id]
		stored = service.store.get_message(allowed.id)
		assert len(stored.body) == 128 and stored.truncated
		assert service.store.get_message(recent.id) is None
	finally:
		service.close()


def test_context_body_privacy_applies_effective_frozen_exclusions_before_fetch(tmp_path):
	config = Config(ai_module="gmail_clean.demo_ai", sent_context_enabled=True, archived_context_enabled=True, body_limit=128)
	service = make_service(tmp_path, config=config)
	try:
		message = service.mailbox.messages[f"d{7:015x}"]
		allowed = Message("context-allowed", message.thread_id, epoch_ms(ANCHOR - timedelta(days=30)),
			labels={"SENT"}, headers={"from": "allowed@example.invalid"}, body="a" * 500, account_id=DEMO_ACCOUNT)
		excluded = Message("context-private", message.thread_id, epoch_ms(ANCHOR - timedelta(days=50)),
			labels={"SENT"}, headers={"from": "private@example.invalid"}, body="private body", account_id=DEMO_ACCOUNT)
		recent = Message("context-recent", message.thread_id, epoch_ms(ANCHOR - timedelta(days=1)),
			labels={"SENT"}, body="recent body", account_id=DEMO_ACCOUNT)
		for item in (allowed, excluded, recent):
			service.mailbox.add_message(item)
		frozen = Config.from_dict(config.to_dict())
		frozen.excluded_senders = ["private@example.invalid"]
		service.mailbox.body_reads.clear()
		result = service.context_messages(message, resolve_window(config, ANCHOR), config=frozen)
		assert [item.id for item in result] == [allowed.id]
		assert len(result[0].body) == 128 and result[0].truncated
		assert service.mailbox.body_reads == [allowed.id]
	finally:
		service.close()


def test_manual_action_rejects_new_protection_but_allows_displayed_bypass(tmp_path):
	service = make_service(tmp_path)
	try:
		message = cache(service, f"d{7:015x}")
		service.mailbox.messages[message.id].labels.add("STARRED")
		assert service.manual([message.id], "archive") == []
		assert service.store.one("SELECT decision FROM messages WHERE id=?", (message.id,))["decision"] == "review"
		assert service.store.one("SELECT count(*) n FROM actions")["n"] == 0
		assert service.mailbox.mutations == []
		# After the starred state is actually displayed, a direct command can
		# deliberately override it; the override records exactly that protection.
		message = cache(service, message.id)
		actions = service.manual([message.id], "archive")
		assert len(actions) == 1
		payload = json.loads(service.store.one("SELECT payload_json FROM actions WHERE id=?", (actions[0],))["payload_json"])
		assert payload["protection_bypass"] == ["starred"]
	finally:
		service.close()


def test_manual_settings_or_context_snapshot_change_requires_reconsideration(tmp_path):
	service = make_service(tmp_path)
	try:
		message = cache(service, f"d{7:015x}")
		revision = service.config.revision
		updated = Config.from_dict(service.config.to_dict())
		updated.protected_senders = [message.sender]
		service.update_config(updated)
		with pytest.raises(ValueError, match="Settings changed"):
			service.manual([message.id], "archive", displayed_revision=revision)
		assert service.manual([message.id], "archive", displayed_context_versions={message.id: "old-display-context"}) == []
		assert service.mailbox.mutations == []
	finally:
		service.close()


def test_bulk_review_has_one_feedback_group_and_retention_keeps_undo_history(tmp_path):
	service = make_service(tmp_path)
	try:
		messages = [cache(service, f"d{index:015x}") for index in (3, 5, 7)]
		service.manual([item.id for item in messages], "archive")
		assert service.store.one("SELECT count(DISTINCT group_id) n FROM feedback")["n"] == 1
		assert service.store.one("SELECT count(*) n FROM actions")["n"] == 3
		service.store.execute("UPDATE messages SET body_cached_at='2000-01-01T00:00:00+00:00'")
		assert service.retention()["bodies_removed"] == 3
		assert service.store.one("SELECT count(*) n FROM actions")["n"] == 3
		assert all(not service.store.get_message(item.id).body for item in messages)
	finally:
		service.close()


def test_mistaken_archive_explicit_feedback_undo_uses_original_ids_after_scope_narrows(tmp_path):
	config = Config(ai_module="gmail_clean.demo_ai", dry_run=False)
	service = make_service(tmp_path, config=config)
	try:
		message = cache(service, f"d{7:015x}")
		before = set(message.labels)
		action_ids = service.manual([message.id], "archive")
		assert "INBOX" not in service.mailbox.get_message(message.id).labels
		config = Config.from_dict(service.config.to_dict())
		config.earliest_date = "2026-10-01"
		service.update_config(config)
		service.mailbox.body_reads.clear()
		service.manual([message.id], "mistaken_archive")
		assert service.mailbox.get_message(message.id).labels == before
		assert service.mailbox.body_reads == []
		assert service.store.one("SELECT state FROM actions WHERE id=?", (action_ids[0],))["state"] == "undone"
		feedback = service.store.one("SELECT action,correction_of FROM feedback WHERE action='mistaken_archive'")
		assert feedback["correction_of"] == action_ids[0]
	finally:
		service.close()


def test_demo_external_ai_is_rejected_before_import_or_execution(tmp_path):
	from gmail_clean.ai import AIError
	service = make_service(tmp_path)
	try:
		config = Config.from_dict(service.config.to_dict())
		config.ai_module = "private_network_module"
		with pytest.raises(AIError, match="Offline demo"):
			service.update_config(config)
		assert service.config.ai_module == "gmail_clean.demo_ai"
		service.config.ai_module = "private_network_module"
		with pytest.raises(AIError, match="Offline demo"):
			service.ai_for()
		assert service._clients == {}
	finally:
		service.close()


def test_retry_errors_scopes_work_resets_attempts_and_preserves_user_pause(tmp_path):
	service = make_service(tmp_path)
	try:
		first, other = service.new_job(), service.new_job()
		for job in (first, other):
			service.store.execute("INSERT INTO work_items(job_id,message_id,stage,state,attempts,error) VALUES(?,'m','body','failed',3,'old')", (job,))
		service.control_job(first, "paused")
		service.control_job(other, "paused")
		result = service.retry_errors(job_id=first)
		assert result["queued"] == 1
		rows = {row["job_id"]: row for row in service.store.query("SELECT * FROM work_items")}
		assert rows[first]["state"] == "pending" and rows[first]["attempts"] == 0
		assert rows[other]["state"] == "failed" and rows[other]["attempts"] == 3
		assert service.jobs.summary(first)["state"] == "paused"
		assert service.jobs.summary(other)["state"] == "paused"
		with pytest.raises(ValueError, match="job or work-item"):
			service.retry_errors()
	finally:
		service.close()


def test_connection_state_cache_reuses_unchanged_file_and_detects_replace(tmp_path, monkeypatch):
	from pathlib import Path
	service = make_service(tmp_path, mailbox=FakeLive())
	service._write_identity()
	reads = []
	original = Path.read_text
	def tracked(path, *args, **kwargs):
		if path.name == "account.json":
			reads.append(path)
		return original(path, *args, **kwargs)
	monkeypatch.setattr(Path, "read_text", tracked)
	try:
		for _ in range(10):
			assert service.mailbox is service._mailbox
		assert len(reads) <= 1
		path = tmp_path / "account.json"
		state = json.loads(original(path))
		state["connected"] = False
		replacement = tmp_path / "replacement.json"
		replacement.write_text(json.dumps(state))
		replacement.replace(path)
		with pytest.raises(AuthenticationError):
			_ = service.mailbox
	finally:
		service.close()


def test_retry_errors_selection_does_not_reset_other_rows(tmp_path):
	service = make_service(tmp_path)
	try:
		job = service.new_job()
		for mid in ("selected", "other"):
			service.store.execute("INSERT INTO work_items(job_id,message_id,stage,state,attempts) VALUES(?,?,'metadata','retry',3)", (job, mid))
		selected = service.store.one("SELECT id FROM work_items WHERE message_id='selected'")["id"]
		service.retry_errors(work_item_ids=[selected])
		assert service.store.one("SELECT attempts FROM work_items WHERE message_id='selected'")["attempts"] == 0
		assert service.store.one("SELECT attempts FROM work_items WHERE message_id='other'")["attempts"] == 3
	finally:
		service.close()


def test_disconnect_can_explicitly_revoke_google_consent(tmp_path, monkeypatch):
	from gmail_clean.auth import AuthManager
	calls = []
	monkeypatch.setattr(AuthManager, "disconnect", lambda self, revoke=False: calls.append(revoke))
	service = make_service(tmp_path, mailbox=FakeLive())
	try:
		result = service.disconnect(revoke=True)
		assert calls == [True]
		assert "revoked" in result["message"]
		assert not service._connection_state()["connected"]
	finally:
		service.close()


def test_external_ai_fingerprint_is_recorded_and_changed_code_needs_approval(tmp_path):
	from gmail_clean.ai import AIError
	module = tmp_path / "provider.py"
	module.write_text("def call_api(prompt):\n\treturn 'original'\n")
	service = make_service(tmp_path / "data", config=Config(ai_module=str(module)), mailbox=FakeLive())
	try:
		with pytest.raises(AIError, match="no approved"):
			_ = service.ai_for()
		service.update_config(service.config, approve_ai_module=True)
		assert len(service.config.ai_module_sha256) == 64
		first_hash = service.config.ai_module_sha256
		_ = service.ai_for()
		module.write_text("def call_api(prompt):\n\treturn 'changed'\n")
		with pytest.raises(AIError, match="changed"):
			_ = service.ai_for()
		with pytest.raises(AIError, match="changed"):
			service.update_config(service.config)
		service.update_config(service.config, approve_ai_module=True)
		assert service.config.ai_module_sha256 != first_hash
		assert service.store.checkpoint("effective_preferences")["ai_module_sha256"] == service.config.ai_module_sha256
	finally:
		service.close()


def test_session_ai_source_approval_is_verified_and_never_persisted(tmp_path):
	from gmail_clean.ai import module_trust_details
	module = tmp_path / "session_ai.py"
	module.write_text("def call_api(prompt):\n\treturn 'offline fixture'\n", encoding="utf-8")
	service = make_service(tmp_path, mailbox=FakeLive())
	try:
		before = service.store.checkpoint("effective_preferences")
		service.set_session_config(ai_module=str(module), approve_ai_module=True)
		assert service.config.ai_module_sha256 == module_trust_details(str(module))["sha256"]
		assert service.store.checkpoint("effective_preferences") == before
		module.write_text("def call_api(prompt):\n\treturn 'changed'\n", encoding="utf-8")
		with pytest.raises(Exception, match="changed|SHA-256|approved"):
			service.set_session_config(ai_model="another")
	finally:
		service.close()


def test_new_module_keeps_the_exact_fingerprint_confirmed_before_settings_save(tmp_path):
	from gmail_clean.ai import AIError, module_trust_details
	module = tmp_path / "new_provider.py"
	module.write_text("def call_api(prompt):\n\treturn 'reviewed source'\n", encoding="utf-8")
	service = make_service(tmp_path / "data", mailbox=FakeLive())
	try:
		before = service.store.checkpoint("effective_preferences")
		candidate = Config.from_dict(service.config.to_dict())
		candidate.ai_module = str(module)
		candidate.ai_module_sha256 = module_trust_details(str(module))["sha256"]
		module.write_text("def call_api(prompt):\n\treturn 'unreviewed replacement'\n", encoding="utf-8")
		with pytest.raises(AIError, match="changed"):
			service.update_config(candidate)
		assert service.store.checkpoint("effective_preferences") == before
	finally:
		service.close()


def test_connect_preserves_session_overrides_without_saving_them_to_another_account(tmp_path, monkeypatch):
	from gmail_clean.auth import AuthManager
	service = make_service(tmp_path, mailbox=FakeLive())
	base = service.store.checkpoint("effective_preferences")
	service.set_session_config(dry_run=False, max_actions_per_run=7, ai_model="invocation-only")
	monkeypatch.setattr(AuthManager, "connect", lambda self, path: FakeLive("another@example.invalid"))
	try:
		service.connect("synthetic-not-read.json")
		assert service.store.checkpoint("effective_preferences")["dry_run"] == base["dry_run"]
		assert service.store.checkpoint("effective_preferences")["ai_model"] == base["ai_model"]
		assert service.store.checkpoint("effective_preferences")["max_actions_per_run"] == base["max_actions_per_run"]
		assert service.config.dry_run is False
		assert service.config.ai_model == "invocation-only"
		assert service.config.max_actions_per_run == 7
	finally:
		service.close()


def test_first_worker_checkpoint_does_not_include_inherited_session_overrides(tmp_path):
	service = make_service(tmp_path, mailbox=FakeLive())
	service.set_session_config(dry_run=False, ai_model="invocation-only")
	# A newly initialized worker/account database may not yet have preferences.
	service.store.execute("DELETE FROM checkpoints WHERE key='effective_preferences'")
	try:
		with service.worker() as worker:
			assert worker.config.dry_run is False
			assert worker.config.ai_model == "invocation-only"
			assert worker.store.checkpoint("effective_preferences")["dry_run"] is True
			assert worker.store.checkpoint("effective_preferences")["ai_model"] == ""
	finally:
		service.close()


def test_label_catalog_ttl_is_shared_by_workers_and_force_refreshes(tmp_path, monkeypatch):
	mailbox = DemoMailbox(count=40, anchor=ANCHOR)
	calls = []
	original = mailbox.labels
	def counted():
		calls.append(True)
		return original()
	monkeypatch.setattr(mailbox, "labels", counted)
	service = make_service(tmp_path, mailbox=mailbox)
	try:
		for _ in range(5):
			service.refresh_labels()
		with service.worker() as worker:
			worker.refresh_labels()
		assert len(calls) == 1
		service.refresh_labels(force=True)
		assert len(calls) == 2
	finally:
		service.close()


def test_unrelated_session_override_keeps_connected_accounts_own_ai_approval(tmp_path, monkeypatch):
	from gmail_clean.ai import module_trust_details
	from gmail_clean.auth import AuthManager
	from gmail_clean.storage import Store
	module = tmp_path / "account_provider.py"
	module.write_text("def call_api(prompt):\n\treturn 'approved account source'\n", encoding="utf-8")
	service = make_service(tmp_path / "data", mailbox=FakeLive())
	target = "another@example.invalid"
	preferences = Config(ai_module=str(module), ai_module_sha256=module_trust_details(str(module))["sha256"])
	other = Store(service._database(target), target)
	other.set_checkpoint("effective_preferences", preferences.to_dict())
	other.close()
	service.set_session_config(dry_run=False)
	monkeypatch.setattr(AuthManager, "connect", lambda self, path: FakeLive(target))
	try:
		service.connect("synthetic-not-read.json")
		assert service.config.ai_module == str(module)
		assert service.config.ai_module_sha256 == preferences.ai_module_sha256
	finally:
		service.close()


def test_rules_revision_cache_and_phase_override_prefetch(tmp_path, monkeypatch):
	service = make_service(tmp_path)
	try:
		message = cache(service, f"d{7:015x}")
		calls = []
		original = service.learning.rules
		def counted(*args, **kwargs):
			calls.append(True)
			return original(*args, **kwargs)
		monkeypatch.setattr(service.learning, "rules", counted)
		assert service.rules() == []
		assert service.rules() == []
		assert len(calls) == 1
		service.learning.save_rule({"conditions": {"sender": message.sender}, "action": "keep", "enabled": True})
		assert len(service.rules()) == 1
		assert len(calls) == 2
		service.learning.record(message, "keep")
		queries = []
		service.store.conn.set_trace_callback(queries.append)
		with service.policy_batch([message.id]):
			for _ in range(10):
				assert service.override(message)["action"] == "keep"
				assert service.rules()
		assert sum("SELECT * FROM manual_overrides" in sql for sql in queries) == 1
	finally:
		service.close()


def test_bulk_manual_and_download_reuse_thread_metadata(tmp_path, monkeypatch):
	service = make_service(tmp_path)
	try:
		messages = [cache(service, f"d{index:015x}") for index in (3, 5, 7)]
		thread_id = messages[0].thread_id
		for message in messages:
			service.mailbox.messages[message.id].thread_id = thread_id
		for message in messages:
			cache(service, message.id)
		counts = {"single": 0, "bulk": 0, "thread": 0}
		original_bulk = service.mailbox.get_messages
		original_thread = service.mailbox.thread_metadata
		def bulk(ids, body=False):
			counts["bulk"] += 1
			return original_bulk(ids, body)
		def thread(tid):
			counts["thread"] += 1
			return original_thread(tid)
		monkeypatch.setattr(service.mailbox, "get_messages", bulk)
		monkeypatch.setattr(service.mailbox, "thread_metadata", thread)
		# The demo bulk adapter internally calls get_message; count only service
		# calls by recording the caller instead of replacing the adapter method.
		import inspect
		original_single = service.mailbox.get_message
		def single(*args, **kwargs):
			if inspect.currentframe().f_back.f_globals.get("__name__") == "gmail_clean.service":
				counts["single"] += 1
			return original_single(*args, **kwargs)
		monkeypatch.setattr(service.mailbox, "get_message", single)
		assert len(service.manual([message.id for message in messages], "keep")) == 0
		assert counts == {"single": 0, "bulk": 1, "thread": 1}
		counts.update(single=0, bulk=0, thread=0)
		assert service.download_bodies([message.id for message in messages]) == {"downloaded": 3}
		assert counts == {"single": 0, "bulk": 2, "thread": 1}
	finally:
		service.close()


def test_optional_context_uses_one_body_batch_and_shared_phase_cache(tmp_path, monkeypatch):
	config = Config(ai_module="gmail_clean.demo_ai", sent_context_enabled=True)
	service = make_service(tmp_path, config=config)
	try:
		message = service.mailbox.messages[f"d{7:015x}"]
		for index in range(3):
			service.mailbox.add_message(Message(f"sent-{index}", message.thread_id,
				epoch_ms(ANCHOR - timedelta(days=40)), {"SENT"}, body="synthetic context", account_id=DEMO_ACCOUNT))
		batches = []
		original = service.mailbox.get_messages
		def bulk(ids, body=False):
			batches.append((list(ids), body))
			return original(ids, body)
		monkeypatch.setattr(service.mailbox, "get_messages", bulk)
		threads, bodies = {}, {}
		window = resolve_window(config, ANCHOR)
		first = service.context_messages(message, window, threads=threads, bodies=bodies)
		second = service.context_messages(message, window, threads=threads, bodies=bodies)
		assert len(first) == len(second) == 3
		assert batches == [([f"sent-{index}" for index in range(3)], True)]
	finally:
		service.close()
