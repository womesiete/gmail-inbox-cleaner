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
				assert worker.mailbox is not service._mailbox
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
		service.store.execute("UPDATE messages SET updated_at='2000-01-01T00:00:00+00:00'")
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
