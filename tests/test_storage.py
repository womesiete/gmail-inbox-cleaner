from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from gmail_clean.domain import Message
from gmail_clean.storage import AccountMismatchError, LeaseBusyError, SCHEMA_VERSION, Store


def message(mid="m1"):
	return Message(mid, "t1", 1_700_000_000_000, {"INBOX", "UNREAD"}, {"From": "Alice <alice@example.invalid>", "Subject": "Receipt"}, body="Receipt body", content_hash="content", context_version="context", account_id="me@example.invalid")


def test_account_binding_and_operational_row_identity(tmp_path):
	path = tmp_path / "mail.sqlite3"
	with Store(path, "me@example.invalid") as store:
		store.execute("INSERT INTO jobs(id) VALUES('j1')")
		assert store.one("SELECT account_id FROM jobs WHERE id='j1'")["account_id"] == "me@example.invalid"
		with pytest.raises(sqlite3.IntegrityError, match="account mismatch"):
			store.execute("INSERT INTO jobs(id,account_id) VALUES('j2','someone@example.invalid')")
	with pytest.raises(AccountMismatchError):
		Store(path, "someone@example.invalid")
	with Store(path, "ME@example.invalid") as store:
		assert store.account_id == "me@example.invalid"


def test_nested_transaction_rollback_does_not_lose_outer_work(tmp_path):
	with Store(tmp_path / "db", "me@example.invalid") as store:
		with store.transaction():
			store.execute("INSERT INTO jobs(id) VALUES('outer')")
			with pytest.raises(RuntimeError):
				with store.transaction():
					store.execute("INSERT INTO jobs(id) VALUES('inner')")
					raise RuntimeError("simulate stage failure")
		assert store.one("SELECT id FROM jobs WHERE id='outer'")
		assert not store.one("SELECT id FROM jobs WHERE id='inner'")
		with pytest.raises(RuntimeError):
			with store.transaction():
				store.execute("INSERT INTO jobs(id) VALUES('rolled-back')")
				raise RuntimeError("simulate crash before commit")
		assert not store.one("SELECT id FROM jobs WHERE id='rolled-back'")


def test_message_roundtrip_and_metadata_refresh_preserves_body(tmp_path):
	with Store(tmp_path / "db", "me@example.invalid") as store:
		mail = message()
		store.upsert_message(mail)
		store.upsert_message(replace(mail, body=None, missing_content=True, labels={"INBOX", "STARRED"}, content_hash=""))
		loaded = store.get_message(mail.id)
		assert loaded.body == "Receipt body"
		assert loaded.labels == {"INBOX", "STARRED"}
		assert loaded.content_hash == "content"
		assert store.one("SELECT sender FROM messages WHERE id=?", (mail.id,))["sender"] == "alice@example.invalid"
		with pytest.raises(AccountMismatchError):
			store.upsert_message(replace(mail, id="foreign", account_id="someone@example.invalid"))


def test_labels_survive_rename_and_deleted_labels_are_retained_for_undo(tmp_path):
	with Store(tmp_path / "db", "me@example.invalid") as store:
		store.replace_labels([{"id": "L1", "name": "Records", "type": "user"}, {"id": "L2", "name": "Old", "type": "user"}])
		store.execute("UPDATE labels SET meaning='reference' WHERE id='L1'")
		store.replace_labels([{"id": "L1", "name": "Filed", "type": "user"}])
		assert store.labels()[0]["name"] == "Filed"
		assert store.labels()[0]["meaning"] == "reference"
		assert len(store.labels(include_deleted=True)) == 2
		assert store.one("SELECT deleted FROM labels WHERE id='L2'")["deleted"] == 1


def test_connection_ownership_is_strict(tmp_path):
	store = Store(tmp_path / "db", "me@example.invalid")
	errors = []

	def use_from_worker():
		try:
			store.one("SELECT 1")
		except RuntimeError as error:
			errors.append(str(error))

	worker = threading.Thread(target=use_from_worker)
	worker.start()
	worker.join()
	assert errors and "another thread" in errors[0]
	store.close()


def test_leases_prevent_duplicate_execution_and_recover_dead_pid(tmp_path, monkeypatch):
	path = tmp_path / "db"
	first = Store(path, "me@example.invalid")
	second = Store(path, "me@example.invalid")
	assert first.acquire_lease("execute")
	assert not second.acquire_lease("execute")
	first.heartbeat("execute")
	with pytest.raises(LeaseBusyError):
		second.require_lease("execute")
	# Even a late heartbeat from a living process must not cause two writers.
	assert not second.acquire_lease("execute", now=datetime.now(timezone.utc) + timedelta(days=2))
	monkeypatch.setattr("gmail_clean.storage._pid_alive", lambda pid: False)
	assert second.acquire_lease("execute")
	second.require_lease("execute")
	with pytest.raises(LeaseBusyError):
		first.heartbeat("execute")
	first.close()
	assert second.one("SELECT name FROM job_leases WHERE name='execute'")
	second.close()


def test_backup_consistent_with_wal_and_cache_pruning_preserves_audit(tmp_path):
	path = tmp_path / "db"
	with Store(path, "me@example.invalid") as store:
		store.upsert_message(message())
		store.execute("INSERT INTO actions(id,message_id,state) VALUES('a1','m1','completed')")
		store.set_checkpoint("history", {"historyId": "42"})
		backup = store.backup(tmp_path / "backup.sqlite3")
		assert store.prune_cache("9999-12-31T23:59:59+00:00") == 1
		assert not store.get_message("m1").body
		assert store.get_message("m1").missing_content
		assert store.one("SELECT state FROM actions WHERE id='a1'")["state"] == "completed"
		assert store.checkpoint("history") == {"historyId": "42"}
	with Store(backup, "me@example.invalid") as restored:
		assert restored.get_message("m1").body == "Receipt body"
		assert restored.checkpoint("history") == {"historyId": "42"}


def test_migrations_and_foreign_keys(tmp_path):
	path = tmp_path / "db"
	with Store(path, "me@example.invalid") as store:
		assert store.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
		assert store.one("PRAGMA foreign_keys")["foreign_keys"] == 1
		assert store.one("PRAGMA busy_timeout")["timeout"] == 5000
		assert store.one("PRAGMA journal_mode")["journal_mode"] == "wal"
		with pytest.raises(sqlite3.IntegrityError):
			store.execute("INSERT INTO attempts(id,action_id) VALUES('attempt','nonexistent')")
	with Store(path, "me@example.invalid") as store:
		assert store.one("PRAGMA integrity_check")["integrity_check"] == "ok"


def test_newer_schema_rejected_without_downgrade(tmp_path):
	path = tmp_path / "newer-db"
	conn = sqlite3.connect(path)
	conn.execute("PRAGMA user_version=999")
	conn.close()
	with pytest.raises(RuntimeError, match="newer"):
		Store(path, "me@example.invalid")


def test_upgrade_from_first_schema_is_transactional(tmp_path):
	from gmail_clean.storage import _MIGRATIONS
	path = tmp_path / "version-one.sqlite3"
	conn = sqlite3.connect(path)
	conn.executescript(_MIGRATIONS[0])
	conn.execute("PRAGMA user_version=1")
	conn.commit()
	conn.close()
	with Store(path, "me@example.invalid") as store:
		assert store.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
		assert store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='examples'")
		store.execute("INSERT INTO jobs(id) VALUES('retained')")
	with Store(path, "me@example.invalid") as store:
		assert store.one("SELECT id FROM jobs WHERE id='retained'")
