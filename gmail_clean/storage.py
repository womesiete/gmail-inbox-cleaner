"""Thread-owned, account-bound SQLite persistence and recoverable job leases.

SQLite and local cache files are not encrypted. The database uses WAL and each
worker opens its own Store; a connection must never cross a thread boundary.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

SCHEMA_VERSION = 4


class AccountMismatchError(RuntimeError):
	"""A database belongs to a different authenticated mailbox."""


class LeaseBusyError(RuntimeError):
	"""Another living owner is executing the job."""


def utc_now() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
	return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


_MIGRATIONS = [
	"""
	CREATE TABLE accounts (
		id TEXT PRIMARY KEY, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
	);
	CREATE TABLE labels (
		id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
		type TEXT NOT NULL DEFAULT 'user', meaning TEXT NOT NULL DEFAULT 'neutral',
		deleted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT '',
		payload_json TEXT NOT NULL DEFAULT '{}'
	);
	CREATE TABLE messages (
		id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', thread_id TEXT NOT NULL DEFAULT '',
		internal_ms INTEGER NOT NULL DEFAULT 0, labels_json TEXT NOT NULL DEFAULT '[]',
		headers_json TEXT NOT NULL DEFAULT '{}', sender TEXT NOT NULL DEFAULT '',
		subject TEXT NOT NULL DEFAULT '', snippet TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
		truncated INTEGER NOT NULL DEFAULT 0, missing_content INTEGER NOT NULL DEFAULT 1,
		content_hash TEXT NOT NULL DEFAULT '', context_version TEXT NOT NULL DEFAULT '',
		in_inbox INTEGER NOT NULL DEFAULT 1, category TEXT NOT NULL DEFAULT '',
		decision TEXT NOT NULL DEFAULT 'pending', defer_until_utc TEXT,
		deadline_utc TEXT, updated_at TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}'
	);
	CREATE TABLE jobs (
		id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
		stage TEXT NOT NULL DEFAULT 'discovery', anchor_utc TEXT NOT NULL DEFAULT '',
		window_json TEXT NOT NULL DEFAULT '{}', config_json TEXT NOT NULL DEFAULT '{}',
		created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
		payload_json TEXT NOT NULL DEFAULT '{}'
	);
	CREATE TABLE work_items (
		id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL DEFAULT '',
		job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
		message_id TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL DEFAULT 'metadata',
		state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
		next_attempt_utc TEXT, error TEXT NOT NULL DEFAULT '', input_version TEXT NOT NULL DEFAULT '',
		payload_json TEXT NOT NULL DEFAULT '{}', UNIQUE(job_id,message_id,stage)
	);
	CREATE TABLE checkpoints (
		key TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', value_json TEXT NOT NULL DEFAULT '{}',
		updated_at TEXT NOT NULL DEFAULT ''
	);
	CREATE TABLE predictions (
		id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL,
		job_id TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '',
		context_version TEXT NOT NULL DEFAULT '', policy_version TEXT NOT NULL DEFAULT '',
		model_version TEXT NOT NULL DEFAULT '', prompt_version TEXT NOT NULL DEFAULT '',
		example_version TEXT NOT NULL DEFAULT '', assessment_json TEXT NOT NULL DEFAULT '{}',
		decision TEXT NOT NULL DEFAULT 'review', reason TEXT NOT NULL DEFAULT '',
		valid INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT '',
		payload_json TEXT NOT NULL DEFAULT '{}'
	);
	CREATE TABLE actions (
		id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL DEFAULT '',
		message_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', action TEXT NOT NULL DEFAULT 'archive',
		add_json TEXT NOT NULL DEFAULT '[]', remove_json TEXT NOT NULL DEFAULT '[]',
		before_json TEXT NOT NULL DEFAULT '{}', after_json TEXT NOT NULL DEFAULT '{}',
		context_version TEXT NOT NULL DEFAULT '', precondition_version TEXT NOT NULL DEFAULT '',
		policy_version TEXT NOT NULL DEFAULT '', approval TEXT NOT NULL DEFAULT 'automatic',
		reason TEXT NOT NULL DEFAULT '', attempt_id TEXT, undo_of TEXT,
		created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
		payload_json TEXT NOT NULL DEFAULT '{}'
	);
	CREATE TABLE attempts (
		id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', action_id TEXT NOT NULL REFERENCES actions(id),
		state TEXT NOT NULL DEFAULT 'pending', before_json TEXT NOT NULL DEFAULT '{}',
		after_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
		started_at TEXT NOT NULL DEFAULT '', finished_at TEXT,
		payload_json TEXT NOT NULL DEFAULT '{}'
	);
	CREATE TABLE errors (
		id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL DEFAULT '',
		message_id TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
		class_name TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
		attempts INTEGER NOT NULL DEFAULT 0, next_attempt_utc TEXT,
		state TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL DEFAULT '',
		payload_json TEXT NOT NULL DEFAULT '{}'
	);
	""",
	"""
	CREATE TABLE feedback (
		id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL,
		action TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual', reason TEXT NOT NULL DEFAULT '',
		scope TEXT NOT NULL DEFAULT 'message', group_id TEXT NOT NULL DEFAULT '',
		context_version TEXT NOT NULL DEFAULT '', previous_prediction TEXT NOT NULL DEFAULT '{}',
		policy_version TEXT NOT NULL DEFAULT '', model_version TEXT NOT NULL DEFAULT '',
		label_ids_json TEXT NOT NULL DEFAULT '[]', supersedes TEXT, correction_of TEXT,
		include_learning INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
		payload_json TEXT NOT NULL DEFAULT '{}'
	);
	CREATE TABLE manual_overrides (
		message_id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', context_version TEXT NOT NULL,
		action TEXT NOT NULL, defer_until_utc TEXT, feedback_id TEXT,
		updated_at TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}'
	);
	CREATE TABLE examples (
		id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', feedback_id TEXT NOT NULL REFERENCES feedback(id),
		message_id TEXT NOT NULL, sender TEXT NOT NULL DEFAULT '', list_id TEXT NOT NULL DEFAULT '',
		category TEXT NOT NULL DEFAULT '', pattern_key TEXT NOT NULL DEFAULT '',
		action TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', label_ids_json TEXT NOT NULL DEFAULT '[]',
		body TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '', context_version TEXT NOT NULL DEFAULT '',
		group_id TEXT NOT NULL DEFAULT '', weight REAL NOT NULL DEFAULT 1.0,
		created_at TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}'
	);
	CREATE TABLE pattern_stats (
		pattern_key TEXT NOT NULL, account_id TEXT NOT NULL DEFAULT '', action TEXT NOT NULL,
		support INTEGER NOT NULL DEFAULT 0, counterexamples INTEGER NOT NULL DEFAULT 0,
		updated_at TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',
		PRIMARY KEY(pattern_key,action)
	);
	CREATE TABLE rules (
		id TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
		conditions_json TEXT NOT NULL DEFAULT '{}', action TEXT NOT NULL DEFAULT 'review',
		label_ids_json TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 0,
		source TEXT NOT NULL DEFAULT 'explicit', revision INTEGER NOT NULL DEFAULT 1,
		support INTEGER NOT NULL DEFAULT 0, counterexamples INTEGER NOT NULL DEFAULT 0,
		created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
		payload_json TEXT NOT NULL DEFAULT '{}'
	);
	CREATE TABLE job_leases (
		name TEXT PRIMARY KEY, account_id TEXT NOT NULL DEFAULT '', owner_token TEXT NOT NULL,
		pid INTEGER NOT NULL, acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
		expires_at TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1
	);
	""",
	"""
	CREATE INDEX idx_messages_inbox_date ON messages(in_inbox,internal_ms,id);
	CREATE INDEX idx_messages_thread ON messages(thread_id,internal_ms);
	CREATE INDEX idx_messages_sender ON messages(sender,internal_ms);
	CREATE INDEX idx_messages_decision ON messages(decision,internal_ms);
	CREATE INDEX idx_messages_due ON messages(defer_until_utc,deadline_utc);
	CREATE INDEX idx_messages_subject ON messages(subject);
	CREATE INDEX idx_jobs_status ON jobs(state,updated_at);
	CREATE INDEX idx_work_queue ON work_items(job_id,stage,state,next_attempt_utc,id);
	CREATE INDEX idx_prediction_lookup ON predictions(message_id,valid,content_hash,context_version);
	CREATE INDEX idx_prediction_policy ON predictions(policy_version,valid);
	CREATE INDEX idx_actions_queue ON actions(state,job_id,created_at);
	CREATE INDEX idx_actions_message ON actions(message_id,created_at);
	CREATE INDEX idx_actions_policy ON actions(policy_version,state);
	CREATE INDEX idx_actions_undo ON actions(undo_of);
	CREATE INDEX idx_attempt_action ON attempts(action_id,started_at);
	CREATE INDEX idx_feedback_message ON feedback(message_id,created_at);
	CREATE INDEX idx_examples_pattern ON examples(pattern_key,created_at);
	CREATE INDEX idx_examples_sender ON examples(sender,category,created_at);
	CREATE INDEX idx_examples_feedback ON examples(feedback_id);
	CREATE INDEX idx_rules_enabled ON rules(enabled,source);
	CREATE INDEX idx_errors_queue ON errors(state,stage,next_attempt_utc);
	""",
	"""
	ALTER TABLE messages ADD COLUMN list_id TEXT NOT NULL DEFAULT '';
	UPDATE messages SET list_id=COALESCE((SELECT lower(trim(CAST(h.value AS TEXT)))
		FROM json_each(messages.headers_json) h WHERE lower(h.key)='list-id' LIMIT 1),'');
	CREATE TRIGGER messages_list_id_insert AFTER INSERT ON messages
	WHEN NEW.list_id != COALESCE((SELECT lower(trim(CAST(h.value AS TEXT)))
		FROM json_each(NEW.headers_json) h WHERE lower(h.key)='list-id' LIMIT 1),'')
	BEGIN
		UPDATE messages SET list_id=COALESCE((SELECT lower(trim(CAST(h.value AS TEXT)))
			FROM json_each(NEW.headers_json) h WHERE lower(h.key)='list-id' LIMIT 1),'') WHERE id=NEW.id;
	END;
	CREATE TRIGGER messages_list_id_update AFTER UPDATE OF headers_json ON messages
	WHEN NEW.list_id != COALESCE((SELECT lower(trim(CAST(h.value AS TEXT)))
		FROM json_each(NEW.headers_json) h WHERE lower(h.key)='list-id' LIMIT 1),'')
	BEGIN
		UPDATE messages SET list_id=COALESCE((SELECT lower(trim(CAST(h.value AS TEXT)))
			FROM json_each(NEW.headers_json) h WHERE lower(h.key)='list-id' LIMIT 1),'') WHERE id=NEW.id;
	END;
	CREATE INDEX idx_messages_list_id ON messages(list_id,id);
	CREATE INDEX idx_examples_list_id ON examples(list_id,created_at);
	CREATE INDEX idx_examples_category ON examples(category,created_at);
	CREATE INDEX idx_rules_pattern_action ON rules(json_extract(payload_json,'$.pattern_key'),action);
	""",
]

_ACCOUNT_TABLES = (
	"labels", "messages", "jobs", "work_items", "checkpoints", "predictions", "actions", "attempts",
	"feedback", "manual_overrides", "examples", "pattern_stats", "rules", "job_leases", "errors",
)


class Store:
	"""An account-bound connection owned by the constructing Python thread.

	Every operational row retains an account identity. Account defaults are filled
	by database triggers, including for direct SQL inserts by core services.
	"""

	def __init__(self, path: str | Path, account_id: str):
		if not account_id or not account_id.strip():
			raise ValueError("An authenticated account identity is required")
		self.path = Path(path) if str(path) != ":memory:" else ":memory:"
		self.account_id = account_id.strip().casefold()
		self._thread_id = threading.get_ident()
		self._owner_token = uuid.uuid4().hex
		self._savepoints = 0
		if self.path != ":memory:":
			self.path.parent.mkdir(parents=True, exist_ok=True)
		self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=5.0)
		self.conn.row_factory = sqlite3.Row
		self.conn.execute("PRAGMA foreign_keys=ON")
		self.conn.execute("PRAGMA busy_timeout=5000")
		self.conn.execute("PRAGMA journal_mode=WAL")
		try:
			self._migrate()
			self._bind_account()
			self._account_triggers()
		except Exception:
			self.conn.close()
			raise
		if self.path != ":memory:" and os.name != "nt":
			os.chmod(self.path, 0o600)

	def _check_thread(self) -> None:
		if threading.get_ident() != self._thread_id:
			raise RuntimeError("Store connection belongs to another thread; open a separate Store")

	def _migrate(self) -> None:
		# Read the version only after the write lock is held. Two starting workers
		# must not both decide to apply the same migration from a stale version.
		self.conn.execute("BEGIN IMMEDIATE")
		try:
			version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
			if version > SCHEMA_VERSION:
				raise RuntimeError("Database schema is newer than this application; use a matching version")
			for number in range(version, SCHEMA_VERSION):
				# complete_statement keeps trigger bodies and quoted semicolons
				# intact while retaining the migration's enclosing transaction.
				statement = ""
				for line in _MIGRATIONS[number].splitlines(keepends=True):
					statement += line
					if sqlite3.complete_statement(statement):
						self.conn.execute(statement)
						statement = ""
				if statement.strip():
					self.conn.execute(statement)
				self.conn.execute(f"PRAGMA user_version={number + 1}")
			self.conn.commit()
		except Exception:
			if self.conn.in_transaction:
				self.conn.rollback()
			raise

	def _bind_account(self) -> None:
		with self.transaction():
			rows = self.query("SELECT id FROM accounts")
			if rows and (len(rows) != 1 or rows[0]["id"] != self.account_id):
				raise AccountMismatchError("The local database belongs to a different Gmail account")
			now = utc_now()
			self.execute("INSERT INTO accounts(id,created_at,last_seen_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at", (self.account_id, now, now))

	def _account_triggers(self) -> None:
		# A single-account database prevents unsafe cross-account reuse by design.
		for table in _ACCOUNT_TABLES:
			key = "rowid"  # Includes text and composite-primary-key tables.
			self.conn.executescript(f"""
			CREATE TRIGGER IF NOT EXISTS {table}_account_default AFTER INSERT ON {table}
			WHEN NEW.account_id = '' BEGIN
				UPDATE {table} SET account_id=(SELECT id FROM accounts LIMIT 1) WHERE {key}=NEW.{key};
			END;
			CREATE TRIGGER IF NOT EXISTS {table}_account_insert BEFORE INSERT ON {table}
			WHEN NEW.account_id != '' AND NEW.account_id != (SELECT id FROM accounts LIMIT 1)
			BEGIN SELECT RAISE(ABORT,'account mismatch'); END;
			CREATE TRIGGER IF NOT EXISTS {table}_account_update BEFORE UPDATE OF account_id ON {table}
			WHEN NEW.account_id != (SELECT id FROM accounts LIMIT 1)
			BEGIN SELECT RAISE(ABORT,'account mismatch'); END;
			""")

	def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> sqlite3.Cursor:
		self._check_thread()
		return self.conn.execute(sql, params)

	def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[dict[str, Any]]:
		return [dict(row) for row in self.execute(sql, params).fetchall()]

	def one(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> dict[str, Any] | None:
		row = self.execute(sql, params).fetchone()
		return dict(row) if row is not None else None

	@contextlib.contextmanager
	def transaction(self) -> Iterator[Store]:
		self._check_thread()
		nested = self.conn.in_transaction
		self._savepoints += 1
		name = f"store_sp_{self._savepoints}"
		self.conn.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
		try:
			yield self
			self.conn.execute(f"RELEASE SAVEPOINT {name}" if nested else "COMMIT")
		except BaseException:
			if nested:
				self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
				self.conn.execute(f"RELEASE SAVEPOINT {name}")
			elif self.conn.in_transaction:
				self.conn.rollback()
			raise

	def upsert_message(self, message: Any) -> None:
		from email.utils import parseaddr
		data = message.to_dict() if hasattr(message, "to_dict") else dict(message)
		identity = data.get("account_id") or self.account_id
		if identity.casefold() != self.account_id:
			raise AccountMismatchError("Message belongs to another account")
		headers = data.get("headers", {})
		lower = {str(key).lower(): value for key, value in headers.items()}
		labels = sorted(data.get("labels", []))
		old = self.one("SELECT content_hash,body,payload_json FROM messages WHERE id=?", (data["id"],))
		# Metadata refreshes must not silently destroy an already cached body.
		if old and not data.get("body") and data.get("missing_content", True):
			previous = json.loads(old["payload_json"] or "{}")
			if previous.get("body"):
				for field in ("body", "truncated", "missing_content", "content_hash"):
					data[field] = previous.get(field, data.get(field))
		body = data.get("body") or ""
		now = utc_now()
		self.execute("""INSERT INTO messages
			(id,account_id,thread_id,internal_ms,labels_json,headers_json,list_id,sender,subject,snippet,body,
			truncated,missing_content,content_hash,context_version,in_inbox,updated_at,payload_json)
			VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
			thread_id=excluded.thread_id,internal_ms=excluded.internal_ms,labels_json=excluded.labels_json,
			headers_json=excluded.headers_json,list_id=excluded.list_id,sender=excluded.sender,subject=excluded.subject,snippet=excluded.snippet,
			body=excluded.body,truncated=excluded.truncated,missing_content=excluded.missing_content,
			content_hash=excluded.content_hash,context_version=excluded.context_version,in_inbox=excluded.in_inbox,
			updated_at=excluded.updated_at,payload_json=excluded.payload_json""", (
			data["id"], self.account_id, data.get("thread_id", ""), data.get("internal_ms", 0),
			canonical_json(labels), canonical_json(headers), str(lower.get("list-id", "")).strip().casefold(), parseaddr(str(lower.get("from", "")))[1].casefold(),
			str(lower.get("subject", "")), data.get("snippet", ""), body, int(bool(data.get("truncated", False))),
			int(bool(data.get("missing_content", not body))), data.get("content_hash", ""),
			data.get("context_version", ""), int("INBOX" in labels), now, canonical_json(data),
		))

	def get_message(self, message_id: str) -> Any | None:
		from .domain import Message
		row = self.one("SELECT * FROM messages WHERE id=?", (message_id,))
		if row is None:
			return None
		data = json.loads(row["payload_json"] or "{}")
		data.update({
			"id": row["id"], "account_id": row["account_id"], "thread_id": row["thread_id"],
			"internal_ms": row["internal_ms"], "labels": json.loads(row["labels_json"]),
			"headers": json.loads(row["headers_json"]), "snippet": row["snippet"], "body": row["body"],
			"truncated": bool(row["truncated"]), "missing_content": bool(row["missing_content"]),
			"content_hash": row["content_hash"], "context_version": row["context_version"],
		})
		return Message.from_dict(data)

	def replace_labels(self, labels: list[dict[str, Any]]) -> None:
		with self.transaction():
			self.execute("UPDATE labels SET deleted=1")
			for label in labels:
				self.execute("""INSERT INTO labels(id,name,type,meaning,deleted,updated_at,payload_json)
					VALUES(?,?,?,?,0,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,type=excluded.type,
					deleted=0,updated_at=excluded.updated_at,payload_json=excluded.payload_json""", (
					label["id"], label.get("name", label["id"]), label.get("type", "user"),
					label.get("meaning", "neutral"), utc_now(), canonical_json(label),
				))

	def labels(self, include_deleted: bool = False) -> list[dict[str, Any]]:
		return self.query("SELECT * FROM labels" + ("" if include_deleted else " WHERE deleted=0") + " ORDER BY type,name")

	def checkpoint(self, key: str, default: Any = None) -> Any:
		row = self.one("SELECT value_json FROM checkpoints WHERE key=?", (key,))
		return json.loads(row["value_json"]) if row else default

	def set_checkpoint(self, key: str, value: Any) -> None:
		self.execute("INSERT INTO checkpoints(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (key, canonical_json(value), utc_now()))

	def acquire_lease(self, name: str, ttl_seconds: int = 90, now: datetime | None = None) -> bool:
		now = now or datetime.now(timezone.utc)
		if now.tzinfo is None:
			raise ValueError("Lease time must be timezone aware")
		with self.transaction():
			row = self.one("SELECT * FROM job_leases WHERE name=?", (name,))
			if row and row["owner_token"] != self._owner_token:
				# A living process keeps ownership even if a long network call delays its
				# heartbeat. A dead process is safe to recover immediately.
				if _pid_alive(int(row["pid"])):
					return False
			stamp = now.astimezone(timezone.utc).isoformat()
			expires = (now + timedelta(seconds=ttl_seconds)).astimezone(timezone.utc).isoformat()
			generation = (int(row["generation"]) + 1) if row and row["owner_token"] != self._owner_token else (int(row["generation"]) if row else 1)
			self.execute("""INSERT INTO job_leases(name,owner_token,pid,acquired_at,heartbeat_at,expires_at,generation)
				VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET owner_token=excluded.owner_token,pid=excluded.pid,
				acquired_at=excluded.acquired_at,heartbeat_at=excluded.heartbeat_at,expires_at=excluded.expires_at,generation=excluded.generation""", (
				name, self._owner_token, os.getpid(), stamp, stamp, expires, generation,
			))
		return True

	def heartbeat(self, name: str, ttl_seconds: int = 90) -> None:
		now = datetime.now(timezone.utc)
		cursor = self.execute("UPDATE job_leases SET heartbeat_at=?,expires_at=? WHERE name=? AND owner_token=?", (now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat(), name, self._owner_token))
		if cursor.rowcount != 1:
			raise LeaseBusyError("Execution lease was lost")

	def require_lease(self, name: str) -> None:
		row = self.one("SELECT owner_token,pid FROM job_leases WHERE name=?", (name,))
		if not row or row["owner_token"] != self._owner_token or row["pid"] != os.getpid():
			raise LeaseBusyError("This worker does not own the execution lease")

	def release_lease(self, name: str) -> None:
		self.execute("DELETE FROM job_leases WHERE name=? AND owner_token=?", (name, self._owner_token))

	def backup(self, destination: str | Path) -> Path:
		self._check_thread()
		if self.conn.in_transaction:
			raise RuntimeError("Finish the active transaction before creating a backup")
		target = Path(destination)
		if self.path != ":memory:" and target.resolve() == Path(self.path).resolve():
			raise ValueError("Backup must use a different file")
		target.parent.mkdir(parents=True, exist_ok=True)
		with sqlite3.connect(str(target)) as other:
			self.conn.backup(other)
		if os.name != "nt":
			os.chmod(target, 0o600)
		return target

	def prune_cache(self, before_utc: str) -> int:
		"""Remove old cached bodies while retaining identifiers and audit/undo data."""
		rows = self.query("SELECT id,payload_json FROM messages WHERE updated_at<? AND body!=''", (before_utc,))
		with self.transaction():
			for row in rows:
				data = json.loads(row["payload_json"] or "{}")
				data["body"] = ""
				data["missing_content"] = True
				self.execute("UPDATE messages SET body='',missing_content=1,payload_json=? WHERE id=?", (canonical_json(data), row["id"]))
		return len(rows)

	def close(self) -> None:
		self._check_thread()
		try:
			# A read-only/main-thread service usually owns no leases. Avoid taking
			# SQLite's writer lock merely to delete zero rows during window close.
			if self.one("SELECT 1 FROM job_leases WHERE owner_token=? LIMIT 1", (self._owner_token,)):
				self.execute("DELETE FROM job_leases WHERE owner_token=?", (self._owner_token,))
		finally:
			self.conn.close()

	def __enter__(self) -> Store:
		return self

	def __exit__(self, *args: Any) -> None:
		self.close()


def _pid_alive(pid: int) -> bool:
	if pid <= 0:
		return False
	if os.name == "nt":
		return _windows_pid_alive(pid)
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	except PermissionError:
		return True
	except OSError:
		return False
	return True


def _windows_process_api():
	"""Load Win32 functions lazily so importing the store stays portable."""
	import ctypes
	return ctypes.WinDLL("kernel32", use_last_error=True), ctypes.get_last_error


def _windows_pid_alive(pid: int) -> bool:
	"""Query a process without sending a signal or requesting termination rights.

	Windows ``os.kill(pid, 0)`` does not provide the POSIX existence probe. Use
	only limited query access, and retain the lease whenever access/query errors
	leave the owner's state unknown. Only a missing or exited process is dead.
	"""
	import ctypes
	from ctypes import wintypes

	kernel32, last_error = _windows_process_api()
	open_process = kernel32.OpenProcess
	open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
	open_process.restype = wintypes.HANDLE
	get_exit_code = kernel32.GetExitCodeProcess
	get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
	get_exit_code.restype = wintypes.BOOL
	close_handle = kernel32.CloseHandle
	close_handle.argtypes = [wintypes.HANDLE]
	close_handle.restype = wintypes.BOOL

	# PROCESS_QUERY_LIMITED_INFORMATION; never PROCESS_TERMINATE or ALL_ACCESS.
	handle = open_process(0x1000, False, pid)
	if not handle:
		# OpenProcess uses ERROR_INVALID_PARAMETER for a nonexistent process ID.
		# Access denied and other unknown failures must not steal a live lease.
		return last_error() != 87
	try:
		exit_code = wintypes.DWORD()
		if not get_exit_code(handle, ctypes.byref(exit_code)):
			return True
		return exit_code.value == 259  # STILL_ACTIVE
	finally:
		close_handle(handle)
