from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
	# An expired owner is fenced even when its PID still exists.
	assert second.acquire_lease("execute", now=datetime.now(timezone.utc) + timedelta(days=2))
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


@pytest.mark.parametrize("exit_code,query_succeeds,expected", [(259, True, True), (0, True, False), (1, True, False), (0, False, True)])
def test_windows_lease_probe_uses_query_only_access_and_closes_handle(monkeypatch, exit_code, query_succeeds, expected):
	from gmail_clean import storage

	class NativeFunction:
		def __init__(self, function):
			self.function = function

		def __call__(self, *args):
			return self.function(*args)

	calls = []
	def open_process(access, inherit, pid):
		calls.append(("open", access, inherit, pid))
		return 0x123456789  # Exercise an opaque handle wider than a 32-bit integer.

	def get_exit_code(handle, pointer):
		calls.append(("query", handle))
		pointer._obj.value = exit_code
		return query_succeeds

	api = SimpleNamespace(OpenProcess=NativeFunction(open_process), GetExitCodeProcess=NativeFunction(get_exit_code),
		CloseHandle=NativeFunction(lambda handle: calls.append(("close", handle)) or True))
	monkeypatch.setattr(storage, "_windows_process_api", lambda: (api, lambda: 0))
	def forbidden_signal(*args):
		raise AssertionError("A Windows liveness check must never signal a process")
	monkeypatch.setattr(storage, "os", SimpleNamespace(name="nt", kill=forbidden_signal))
	assert storage._pid_alive(1234) is expected
	assert calls == [("open", 0x1000, False, 1234), ("query", 0x123456789), ("close", 0x123456789)]


@pytest.mark.parametrize("native_error,expected", [(87, False), (5, True), (6, True)])
def test_windows_lease_probe_does_not_recover_a_process_with_unknown_status(monkeypatch, native_error, expected):
	from gmail_clean import storage

	class NativeFunction:
		def __init__(self, function):
			self.function = function

		def __call__(self, *args):
			return self.function(*args)

	def unexpected(*args):
		raise AssertionError("An unopened handle must not be queried or closed")
	api = SimpleNamespace(OpenProcess=NativeFunction(lambda *args: None), GetExitCodeProcess=NativeFunction(unexpected), CloseHandle=NativeFunction(unexpected))
	monkeypatch.setattr(storage, "_windows_process_api", lambda: (api, lambda: native_error))
	assert storage._windows_pid_alive(1234) is expected


def test_current_process_lease_probe_is_non_destructive():
	# Run in a child so a future Windows os.kill regression becomes a failed
	# assertion rather than silently terminating the entire pytest process.
	result = subprocess.run([sys.executable, "-c",
		"import os; from gmail_clean.storage import _pid_alive; assert _pid_alive(os.getpid()); print('owner-is-alive')"],
		capture_output=True, text=True, timeout=30, check=False)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "owner-is-alive"


def test_list_id_upgrade_backfills_mixed_case_headers_and_remains_indexed(tmp_path):
	from gmail_clean.storage import _MIGRATIONS
	path = tmp_path / "version-three.sqlite3"
	conn = sqlite3.connect(path)
	for migration in _MIGRATIONS[:3]:
		conn.executescript(migration)
	conn.execute("INSERT INTO messages(id,headers_json) VALUES('old','{\"lIsT-iD\":\"  Weekly.Example.INVALID  \"}')")
	conn.execute("PRAGMA user_version=3")
	conn.commit()
	conn.close()
	with Store(path, "me@example.invalid") as store:
		assert store.one("SELECT list_id FROM messages WHERE id='old'")["list_id"] == "weekly.example.invalid"
		plan = store.query("EXPLAIN QUERY PLAN SELECT id FROM messages WHERE list_id=?", ("weekly.example.invalid",))
		assert any("idx_messages_list_id" in row["detail"] for row in plan)
		store.execute("UPDATE messages SET headers_json='{}' WHERE id='old'")
		assert store.one("SELECT list_id FROM messages WHERE id='old'")["list_id"] == ""
		store.upsert_message(replace(message("new"), headers={"From": "news@example.invalid", "LIST-ID": "  Digest.Example  "}))
		assert store.one("SELECT list_id FROM messages WHERE id='new'")["list_id"] == "digest.example"
		assert store.one("PRAGMA integrity_check")["integrity_check"] == "ok"


def test_closing_nonowner_store_does_not_wait_for_another_writer(tmp_path):
	import time
	path = tmp_path / "busy.sqlite3"
	reader = Store(path, "me@example.invalid")
	ready, finish = threading.Event(), threading.Event()
	errors = []
	def writer():
		try:
			with Store(path, "me@example.invalid") as store:
				with store.transaction():
					store.execute("INSERT INTO jobs(id) VALUES('held')")
					ready.set()
					finish.wait(10)
		except BaseException as exc:
			errors.append(exc)
	worker = threading.Thread(target=writer)
	worker.start()
	try:
		assert ready.wait(5)
		start = time.monotonic()
		reader.close()
		assert time.monotonic() - start < 1
	finally:
		finish.set()
		worker.join(5)
	assert not errors


def test_expired_lease_is_fenced_and_break_requires_confirmation(tmp_path):
	path = tmp_path / 'leases.sqlite'
	with Store(path, 'me@example.invalid') as first, Store(path, 'me@example.invalid') as second:
		assert first.acquire_lease('execute', ttl_seconds=1)
		assert first.one('SELECT process_start FROM job_leases')['process_start']
		assert second.acquire_lease('execute', now=datetime.now(timezone.utc) + timedelta(seconds=2))
		with pytest.raises(LeaseBusyError):
			first.require_lease('execute')
		with pytest.raises(ValueError, match='confirm'):
			second.break_lease('execute')
		assert second.break_lease('execute', confirmed=True)


def test_background_heartbeat_renews_lease_without_owner_connection(tmp_path):
	import time
	path = tmp_path / 'heartbeat.sqlite'
	with Store(path, 'me@example.invalid') as store:
		assert store.acquire_lease('execute')
		before = store.one('SELECT heartbeat_at FROM job_leases')['heartbeat_at']
		with store.lease_heartbeat('execute', interval_seconds=0.02):
			time.sleep(0.08)
		assert store.one('SELECT heartbeat_at FROM job_leases')['heartbeat_at'] > before



def test_numbered_migration_has_a_pre_migration_backup(tmp_path):
	from gmail_clean.storage import _MIGRATIONS
	path = tmp_path / 'old.sqlite'
	with sqlite3.connect(path) as connection:
		for script in _MIGRATIONS[:4]:
			connection.executescript(script)
		connection.execute('PRAGMA user_version=4')
	with Store(path, 'me@example.invalid'):
		backups = list(tmp_path.glob('old.sqlite.pre-migration-v4-*.sqlite3'))
		assert len(backups) == 1
		with sqlite3.connect(backups[0]) as backup:
			assert backup.execute('PRAGMA user_version').fetchone()[0] == 4


def test_trigger_ddl_is_migrated_once_and_bulk_upsert_is_atomic(tmp_path, monkeypatch):
	calls = []
	original = Store._account_triggers
	def tracked(self):
		calls.append(True)
		return original(self)
	monkeypatch.setattr(Store, '_account_triggers', tracked)
	path = tmp_path / 'triggers.sqlite'
	with Store(path, 'me@example.invalid'):
		pass
	with Store(path, 'me@example.invalid') as store:
		assert len(calls) == 1
		with pytest.raises(AccountMismatchError):
			store.upsert_messages([message('ok'), replace(message('bad'), account_id='wrong@example.invalid')])
		assert store.get_message('ok') is None
		assert store.one('PRAGMA synchronous')['synchronous'] == 2


def test_cached_bodies_are_single_copy_and_expire_independent_of_refresh(tmp_path):
	with Store(tmp_path / 'retention.sqlite', 'me@example.invalid') as store:
		store.upsert_message(message())
		assert 'body' not in json.loads(store.one('SELECT payload_json FROM messages')['payload_json'])
		store.execute("UPDATE messages SET body_cached_at='2000-01-01T00:00:00+00:00'")
		store.upsert_message(replace(message(), body=None, missing_content=True, content_hash=''))
		assert store.get_message('m1').body == 'Receipt body'
		store.execute("INSERT INTO feedback(id,message_id,action,created_at) VALUES('f','m1','keep','2000-01-01')")
		store.execute("INSERT INTO examples(id,feedback_id,message_id,action,created_at,body) VALUES('e','f','m1','keep','2000-01-01','example body')")
		assert store.prune_cache('2020-01-01T00:00:00+00:00') == 1
		assert not store.one('SELECT body FROM examples')['body']
		assert not store.get_message('m1').body
		assert store.one('PRAGMA secure_delete')['secure_delete'] == 1



def test_housekeeping_keeps_active_work_and_audit_rows(tmp_path):
	with Store(tmp_path / 'housekeeping.sqlite', 'me@example.invalid') as store:
		store.execute("INSERT INTO jobs(id,state,updated_at) VALUES('old','completed','2000-01-01'),('active','paused','2000-01-01')")
		store.execute("INSERT INTO work_items(job_id,message_id,state) VALUES('old','m','completed'),('active','n','completed')")
		store.execute("INSERT INTO errors(id,state,created_at) VALUES('resolved','closed','2000-01-01'),('open','open','2000-01-01')")
		store.execute("INSERT INTO predictions(id,message_id,valid,created_at) VALUES('invalid','m',0,'2000-01-01'),('valid','m',1,'2000-01-01')")
		assert store.housekeeping('2020-01-01') == {'work_items': 1, 'errors': 1, 'predictions': 1}
		assert store.one("SELECT id FROM work_items WHERE job_id='active'")
		assert store.one("SELECT id FROM errors WHERE id='open'")
