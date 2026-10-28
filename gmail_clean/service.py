"""Application services shared by the desktop UI, CLI, and offline tests."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import warnings
from datetime import datetime, timedelta
from pathlib import Path

from .actions import ActionExecutor
from .ai import AIClient, AIError, module_trust_details, verify_module_trust
from .domain import Assessment, Config, Window, content_excluded, content_hash, context_hash, eligible, protections, recommend, resolve_window
from .jobs import JobRunner
from .learning import Learning
from .runtime import data_directory, digest, encode, identifier, insert, path_lock, safe_error, stamp, utc_now
from .storage import LeaseBusyError, Store


class AppService:
	def __init__(self, store=None, mailbox=None, config=None, data_dir=None, demo=False, count=3000, clock=utc_now, ai_factory=None, _worker_owner=None):
		self.demo = demo
		self._closed = False
		self.clock = clock
		self.count = count
		self.data_dir = Path(data_dir) if data_dir else data_directory(demo)
		self.data_dir.mkdir(parents=True, exist_ok=True)
		self._identity_lock = path_lock(self.data_dir / "account.json")
		if os.name != "nt":
			os.chmod(self.data_dir, 0o700)
		self._mailbox = mailbox
		from .gmail import GmailMailbox
		self._supplied_mailbox = mailbox is not None and not isinstance(mailbox, GmailMailbox)
		self._ai_factory = ai_factory
		# Only isolated AI processes are reused between Qt tasks. Each task still
		# owns and closes its SQLite connection on the thread that opened it.
		self._resource_owner = _worker_owner._resource_owner if _worker_owner is not None else self
		if _worker_owner is None:
			self._clients = {}
			self._clients_lock = threading.RLock()
			self._active_workers = set()
			self._workers_lock = threading.RLock()
			self._abort_event = threading.Event()
		else:
			self._clients = self._resource_owner._clients
			self._clients_lock = self._resource_owner._clients_lock
		self._config_overrides = dict(getattr(self._resource_owner, "_config_overrides", {}))
		self._session_write_confirmation = getattr(self._resource_owner, "_session_write_confirmation", False)
		self._cancel_event = threading.Event()
		self._lease_depth = 0
		self.current_job_id = None
		if demo and mailbox is not None:
			from .demo import DemoMailbox
			if not isinstance(mailbox, DemoMailbox):
				raise ValueError("Demo accepts only the offline DemoMailbox adapter")
		if demo and mailbox is None:
			from .demo import DemoMailbox
			self._mailbox = DemoMailbox(self.data_dir / "demo-mailbox.json", count=count, anchor=clock())
		if store is not None:
			self.account_id = store.account_id
			self.store = store
		else:
			self.account_id = self._identity_from_disk()
			if self._mailbox is not None:
				self.account_id = self._mailbox.identity().casefold()
			self.store = Store(self._database(self.account_id), self.account_id)
		self._connection_generation = self._connection_state().get("generation", "")
		saved = self.store.checkpoint("effective_preferences")
		self.config = Config.from_dict(config.to_dict()) if config is not None else Config.from_dict(saved or {})
		if demo and saved is None:
			self.config.label_meanings.setdefault("label_action", "requires_attention")
		if demo and not self.config.ai_module:
			self.config.ai_module = "gmail_clean.demo_ai"
		self._validate_demo_ai(self.config)
		self.config.validate()
		if config is not None or saved is None:
			self.store.set_checkpoint("effective_preferences", self.config.to_dict())
		# Worker/account initialization persists only base preferences. Invocation
		# overlays remain in memory even when this is the first checkpoint.
		self.config = Config.from_dict({**self.config.to_dict(), **self._config_overrides})
		self._validate_demo_ai(self.config)
		self.config.validate()
		self._configure_services()
		if self._mailbox is not None:
			self._check_identity()
			self.refresh_labels()

	def _configure_services(self):
		self.learning = Learning(self.store, enabled=self.config.learning_enabled)
		self.actions = ActionExecutor(self)
		self.jobs = JobRunner(self)

	def _identity_from_disk(self):
		if self.demo:
			return "demo@example.invalid"
		try:
			return self._connection_state()["account_id"].casefold()
		except (FileNotFoundError, ValueError, KeyError):
			return "unconnected"

	def _database(self, account):
		return self.data_dir / ("unconnected.sqlite" if account == "unconnected" else "mailbox-" + digest(account)[:12] + ".sqlite")

	def _connection_state(self):
		with self._identity_lock:
			try:
				return json.loads((self.data_dir / "account.json").read_text(encoding="utf-8"))
			except (FileNotFoundError, ValueError):
				return {}

	def _write_identity(self, connected=True):
		with self._identity_lock:
			target = self.data_dir / "account.json"
			temporary = target.with_name(f"{target.name}.{os.getpid()}.{identifier()[:8]}.tmp")
			generation = identifier()
			try:
				with temporary.open("w", encoding="utf-8") as stream:
					stream.write(encode({"account_id": self.account_id, "connected": connected, "generation": generation}))
					stream.flush()
					os.fsync(stream.fileno())
				temporary.replace(target)
			finally:
				temporary.unlink(missing_ok=True)
			self._connection_generation = generation

	def refresh_connection(self) -> bool:
		account = self._identity_from_disk()
		generation = self._connection_state().get("generation", "")
		if account == self.account_id:
			self.reload_config()
			if generation == self._connection_generation:
				return False
			self._close_ai()
			self._mailbox = None
			self._connection_generation = generation
			return True
		self._close_ai()
		self.store.close()
		self._mailbox = None
		self.account_id = account
		self._connection_generation = generation
		self.store = Store(self._database(account), account)
		saved = self.store.checkpoint("effective_preferences")
		if saved:
			self.config = Config.from_dict(saved)
		else:
			self.store.set_checkpoint("effective_preferences", self.config.to_dict())
		self._configure_services()
		return True

	@property
	def mailbox(self):
		if self._closed:
			raise RuntimeError("This service has been closed")
		if not self.demo:
			state = self._connection_state()
			if state and (not state.get("connected", True) or state.get("generation", "") != self._connection_generation or state.get("account_id", "").casefold() != self.account_id):
				from .auth import AuthenticationError
				raise AuthenticationError("The Gmail connection was disconnected or changed. Connect again or open a fresh worker.")
		if self._mailbox is None:
			from .auth import AuthManager
			self._mailbox = AuthManager(self.data_dir, retrieval_batch_size=self.config.retrieval_batch_size, execution_batch_size=self.config.execution_batch_size, body_limit=self.config.body_limit).mailbox()
			self._check_identity()
		from .gmail import GmailMailbox
		if isinstance(self._mailbox, GmailMailbox):
			self._mailbox.body_limit = self.config.body_limit
			self._mailbox.retrieval_batch_size = self.config.retrieval_batch_size
			self._mailbox.execution_batch_size = self.config.execution_batch_size
		return self._mailbox

	def _check_identity(self):
		if self.demo:
			from .demo import DEMO_ACCOUNT, DemoMailbox
			if not isinstance(self._mailbox, DemoMailbox) or self.account_id != DEMO_ACCOUNT:
				raise ValueError("Demo cannot use a real mailbox")
		if self._mailbox.identity().casefold() != self.account_id:
			raise ValueError("The authenticated account differs from this database; reconnect explicitly")

	@property
	def ai(self):
		return self.ai_for()

	def ai_for(self, category=""):
		self._validate_demo_ai(self.config)
		if not self.demo and self.config.ai_module and self.config.ai_module != "gmail_clean.demo_ai":
			verify_module_trust(self.config.ai_module, self.config.ai_module_sha256)
		if self._closed or self._resource_owner._abort_event.is_set():
			raise AIError("InterruptedRequest", "Application shutdown stopped new local AI work; remote completion of an active request remains unknown.")
		model = self.config.optional_model_routes.get(category, self.config.ai_model)
		key = (self.config.ai_module, self.config.ai_module_sha256, model, self.config.ai_use_call_api_result, self.config.ai_timeout_seconds)
		with self._clients_lock:
			if key not in self._clients:
				factory = self._ai_factory or AIClient
				trust = {"expected_sha256": self.config.ai_module_sha256} if not self.demo and self.config.ai_module and self.config.ai_module != "gmail_clean.demo_ai" else {}
				self._clients[key] = factory(self.config.ai_module, model=model, use_call_api_result=self.config.ai_use_call_api_result, timeout_s=self.config.ai_timeout_seconds, **trust)
			return self._clients[key]

	def _validate_demo_ai(self, config):
		if self.demo and config.ai_module != "gmail_clean.demo_ai":
			raise AIError("ConfigurationError", "Offline demo uses only gmail_clean.demo_ai. Launch Personal mode to configure an external AI module.")

	def force_session_simulation(self):
		"""Restrict one worker to read-only mode without approving or loading AI code."""
		self._config_overrides["dry_run"] = True
		self.config.dry_run = True
		self.reload_config()

	def set_session_config(self, *, approve_ai_module=False, **overrides):
		"""Apply invocation-only preferences without writing a live flag to disk."""
		candidate = Config.from_dict({**self.config.to_dict(), **overrides})
		if "ai_module" in overrides and candidate.ai_module != self.config.ai_module and "ai_module_sha256" not in overrides:
			# A copied fingerprint belongs to the old location, not this selection.
			candidate.ai_module_sha256 = ""
		candidate.validate()
		self._validate_demo_ai(candidate)
		self._approve_ai_source(candidate, self.config, approve_ai_module)
		if approve_ai_module or "ai_module" in overrides or "ai_module_sha256" in overrides:
			overrides["ai_module_sha256"] = candidate.ai_module_sha256
		self._config_overrides.update(overrides)
		self.reload_config()

	def reload_config(self):
		value = self.store.checkpoint("effective_preferences")
		if value:
			self.config = Config.from_dict({**value, **self._config_overrides})
			self.learning.enabled = self.config.learning_enabled

	def _approve_ai_source(self, config, previous, approve_ai_module=False):
		"""Use the same source approval checks for saved and invocation preferences."""
		if config.ai_module and config.ai_module != "gmail_clean.demo_ai":
			if approve_ai_module or (config.ai_module != previous.ai_module and not config.ai_module_sha256):
				trust = module_trust_details(config.ai_module)
				config.ai_module_sha256 = trust["sha256"]
				for warning in trust["warnings"]:
					warnings.warn(warning, RuntimeWarning, stacklevel=2)
			else:
				# Settings may already carry the exact source the user confirmed.
				# Never replace that approval with bytes changed while work queued.
				verify_module_trust(config.ai_module, config.ai_module_sha256)

	def update_config(self, config, *, approve_ai_module=False):
		if isinstance(config, dict):
			config = Config.from_dict(config)
		else:
			config = Config.from_dict(config.to_dict())
		config.validate()
		self._validate_demo_ai(config)
		resolve_window(config, self.clock())
		previous = Config.from_dict(self.store.checkpoint("effective_preferences", self.config.to_dict()))
		self._approve_ai_source(config, previous, approve_ai_module)
		self.config = config
		with self.store.transaction():
			self.store.set_checkpoint("effective_preferences", config.to_dict())
			if previous.revision != config.revision:
				self.store.execute("UPDATE actions SET state='stale',reason='Settings changed; replan',updated_at=? WHERE state IN ('queued','proposed','retry')", (stamp(),))
		self.learning.enabled = config.learning_enabled
		if (previous.ai_module, previous.ai_module_sha256, previous.ai_model, previous.ai_use_call_api_result, previous.ai_timeout_seconds, previous.optional_model_routes) != (config.ai_module, config.ai_module_sha256, config.ai_model, config.ai_use_call_api_result, config.ai_timeout_seconds, config.optional_model_routes):
			self._close_ai()
		return config.to_dict()

	@contextlib.contextmanager
	def worker(self):
		"""Each Qt worker constructs fresh SQLite and Gmail clients on its own thread."""
		owner = self._resource_owner
		if self._closed or owner._abort_event.is_set():
			raise RuntimeError("Application shutdown has stopped new worker operations")
		mailbox = None
		if self._supplied_mailbox:
			# Dependency-injected transports are test-only; production clients are
			# restored inside the worker and never cross thread ownership boundaries.
			mailbox = self._mailbox
			# DemoMailbox synchronizes its public API, so sharing it avoids parsing
			# a 50,000-message state file for every short desktop operation.
		if self.demo:
			mailbox = self.mailbox
		clone = AppService(config=None, data_dir=self.data_dir, demo=self.demo, count=self.count, clock=self.clock, mailbox=mailbox, ai_factory=self._ai_factory, _worker_owner=owner)
		with owner._workers_lock:
			owner._active_workers.add(clone)
		try:
			if owner._abort_event.is_set():
				clone._cancel_event.set()
				raise RuntimeError("Application shutdown stopped this worker before execution")
			yield clone
		finally:
			try:
				clone.close()
			finally:
				with owner._workers_lock:
					owner._active_workers.discard(clone)

	def _stop_callback(self, should_stop=None):
		"""Preserve explicit pause/stop signals while adding local shutdown."""
		def requested():
			signal = should_stop() if should_stop is not None else False
			return signal or self._cancel_event.is_set() or self._resource_owner._abort_event.is_set()
		return requested

	def abort_workers(self):
		"""Stop local AI children without closing worker-owned SQLite connections.

		Network requests can outlive the GUI. Workers retain their connections and
		clean them up on their owning threads; durable attempts cover lost replies.
		"""
		owner = self._resource_owner
		owner._abort_event.set()
		with owner._workers_lock:
			for worker in owner._active_workers:
				worker._cancel_event.set()
		with owner._clients_lock:
			clients = list(owner._clients.values())
		for client in clients:
			abort = getattr(client, "abort", None)
			if abort is not None:
				abort(grace_s=0.1)

	def active_worker_count(self):
		owner = self._resource_owner
		with owner._workers_lock:
			return len(owner._active_workers)

	@contextlib.contextmanager
	def execution_lease(self):
		if self._lease_depth == 0 and not self.store.acquire_lease("mailbox_execution"):
			raise LeaseBusyError("Another worker is using this mailbox; pause it before executing")
		self._lease_depth += 1
		try:
			with self.store.lease_heartbeat("mailbox_execution"):
				yield
		finally:
			self._lease_depth -= 1
			if self._lease_depth == 0:
				self.store.release_lease("mailbox_execution")

	def heartbeat(self, job=None):
		if self._lease_depth:
			self.store.heartbeat("mailbox_execution")

	def new_job(self):
		self.reload_config()
		self.current_job_id = self.jobs.new()
		return self.current_job_id

	def run_job(self, job_id, progress=None, should_stop=None):
		should_stop = self._stop_callback(should_stop)
		if should_stop():
			signal = should_stop()
			self.control_job(job_id, signal if signal in {"paused", "stopped"} else "paused")
			return self.jobs.summary(job_id)
		with self.execution_lease():
			self.reload_config()
			self.current_job_id = job_id
			try:
				self.refresh_labels()
			except Exception as exc:
				self.error("discovery", exc, job_id=job_id)
				self.jobs.handle_exception(job_id, exc, self.config)
				return self.jobs.summary(job_id)
			result = self.jobs.run(job_id, progress, should_stop)
			if result["state"] == "completed":
				self.store.checkpoint_wal(truncate=True)
			return result

	def control_job(self, job_id, state):
		state = {"pause": "paused", "resume": "pending", "stop": "stopped", "running": "pending"}.get(state, state)
		if state not in {"paused", "pending", "stopped"}:
			raise ValueError("Use pause, resume, or stop")
		with self.store.transaction():
			payload = self.jobs.payload(job_id)
			if state == "pending":
				for key in ("pause", "backoff", "system_retries"):
					payload.pop(key, None)
			else:
				payload["pause"] = {"origin": "user", "kind": state, "reason": "Paused by the user" if state == "paused" else "Stopped by the user", "due": None}
			self.jobs.save_payload(job_id, payload)
			self.store.execute("UPDATE jobs SET state=?,updated_at=? WHERE id=?", (state, stamp(), job_id))
		return state

	def eligible(self, message, window):
		if message.account_id and message.account_id.casefold() != self.account_id:
			return False
		current = resolve_window(self.config, window.anchor_utc)
		return eligible(message, window) and eligible(message, current)

	def excluded(self, message):
		config = self.config
		row = self.store.one("SELECT category FROM messages WHERE id=?", (message.id,))
		return (content_excluded(message, config)
			or bool(row and row["category"] in config.excluded_categories))

	def override(self, message):
		override = self.learning.manual_override(message)
		if override and override["action"] == "defer" and override.get("defer_until_utc"):
			if datetime.fromisoformat(override["defer_until_utc"].replace("Z", "+00:00")) <= self.clock():
				self.store.execute("DELETE FROM manual_overrides WHERE message_id=?", (message.id,))
				self.store.execute("UPDATE messages SET defer_until_utc=NULL WHERE id=?", (message.id,))
				return None
		return override

	def _bound_body(self, message, limit=None):
		limit = min(self.config.body_limit, limit) if limit is not None else self.config.body_limit
		if message.body is not None and len(message.body) > limit:
			message.body = message.body[:limit]
			message.truncated = True
		return message

	def examples(self, message):
		return self.learning.examples_for(message, excluded_senders=self.config.excluded_senders, excluded_domains=self.config.excluded_domains, excluded_categories=self.config.excluded_categories, excluded_label_ids=self.config.excluded_label_ids)

	def rules(self):
		return self.learning.rules(include_disabled=False)

	def refresh_labels(self):
		labels = self.mailbox.labels()
		for label in labels:
			label["meaning"] = self.config.label_meanings.get(label["id"], "neutral")
		self.store.replace_labels(labels)
		return labels

	def context_messages(self, message, window, config=None):
		"""Optional context obeys the same body privacy bounds; never action targets."""
		config = config or self.config
		if not (config.sent_context_enabled or config.archived_context_enabled):
			return []
		result = []
		for member in self.mailbox.thread_metadata(message.thread_id):
			if member.id == message.id or not window.contains(member.internal_ms):
				continue
			if not resolve_window(self.config, window.anchor_utc).contains(member.internal_ms) or {"SPAM", "TRASH", "DRAFT"} & member.labels or self.excluded(member) or content_excluded(member, config):
				continue
			row = self.store.one("SELECT category FROM messages WHERE id=?", (member.id,))
			if row and row["category"] in config.excluded_categories:
				continue
			if ("SENT" in member.labels and config.sent_context_enabled and self.config.sent_context_enabled) or ("INBOX" not in member.labels and "SENT" not in member.labels and config.archived_context_enabled and self.config.archived_context_enabled):
				result.append(self._bound_body(self.mailbox.get_message(member.id, body=True), config.body_limit))
			if len(result) >= 8:
				break
		return result

	def download_bodies(self, ids, should_stop=None):
		should_stop = self._stop_callback(should_stop)
		window = resolve_window(self.config, self.clock())
		count = 0
		for mid in ids:
			if should_stop():
				break
			meta = self.mailbox.get_message(mid, body=False)
			if not self.eligible(meta, window) or self.excluded(meta):
				continue
			message = self._bound_body(self.mailbox.get_message(mid, body=True))
			message.context_version = context_hash(self.mailbox.thread_metadata(message.thread_id), self.config.sent_context_enabled)
			message.content_hash = content_hash(message)
			self.store.upsert_message(message)
			count += 1
		return {"downloaded": count}

	def manual(self, ids, action, label_ids=None, reason="", scope="message", defer_until=None, displayed_revision=None, displayed_protections=None, displayed_contexts=None, displayed_context_versions=None, should_stop=None):
		should_stop = self._stop_callback(should_stop)
		displayed_config = self.config
		displayed_revision = displayed_config.revision if displayed_revision is None else displayed_revision
		displayed_contexts = displayed_contexts if displayed_contexts is not None else displayed_context_versions
		self.reload_config()
		if displayed_revision is not None and displayed_revision != self.config.revision:
			raise ValueError("Settings changed since these items were displayed; refresh before reviewing")
		if action not in {"archive", "keep", "defer", "label", "reject_label", "mistaken_archive", "unnecessary_keep"}:
			raise ValueError("Unsupported manual action")
		window = resolve_window(self.config, self.clock())
		group = identifier()
		result = []
		undo_ids = []
		# Explicit selections only: conversation scope describes feedback; it never
		# silently expands the target list to recent or unreviewed thread members.
		# Perform network preflight before opening the bulk feedback transaction:
		# a slow provider must not hold SQLite's writer lock for the whole group.
		validated = []
		for mid in dict.fromkeys(ids):
			if should_stop():
				break
			message = self.store.get_message(mid)
			original = self.store.one("SELECT id FROM actions WHERE message_id=? AND action='archive' AND undo_of IS NULL AND state IN ('applied','reconciled') ORDER BY updated_at DESC LIMIT 1", (mid,)) if action == "mistaken_archive" else None
			# An explicitly identified mistaken app archive uses the ledger's narrow
			# undo exception. It never fetches or classifies an out-of-window body.
			if not message or (not original and not self.eligible(message, window)):
				continue
			live = self.mailbox.get_message(mid, body=False)
			live.context_version = context_hash(self.mailbox.thread_metadata(live.thread_id), self.config.sent_context_enabled)
			expected_context = displayed_contexts.get(mid, "") if displayed_contexts is not None else message.context_version
			if not original and (not self.eligible(live, window) or live.context_version != expected_context or message.context_version != expected_context):
				self.store.execute("UPDATE messages SET decision='review' WHERE id=?", (mid,))
				continue
			bypass = set(displayed_protections.get(mid, [])) if displayed_protections is not None else protections(message, displayed_config)
			if not original and protections(live, self.config) - bypass:
				self.store.execute("UPDATE messages SET decision='review' WHERE id=?", (mid,))
				continue
			validated.append((mid, message, original, bypass))
		with self.learning.batch():
			for mid, message, original, bypass in validated:
				if should_stop():
					break
				prediction = self.store.one("SELECT * FROM predictions WHERE message_id=? ORDER BY created_at DESC LIMIT 1", (mid,))
				category = self.store.one("SELECT category FROM messages WHERE id=?", (mid,))["category"]
				feedback_action = "label_correction" if action == "reject_label" else action
				self.learning.record(message, feedback_action, reason=reason, scope=scope, group_id=group,
					label_ids=[] if action == "reject_label" else (label_ids or []), negative_label_ids=label_ids if action == "reject_label" else [],
					category=category, defer_until_utc=defer_until, previous_prediction=prediction,
					policy_version=self.config.revision, protection_bypass=bypass, correction_of=original["id"] if original else None)
				if original:
					undo_ids.append(original["id"])
				self.store.execute("UPDATE actions SET state='cancelled',reason='Superseded by explicit review',updated_at=? WHERE message_id=? AND state IN ('queued','proposed','stale','retry')", (stamp(), mid))
				if action == "reject_label":
					# Rejection edits local unapplied suggestions, never Gmail's labels.
					for row in self.store.query("SELECT id,assessment_json FROM predictions WHERE message_id=? AND valid=1", (mid,)):
						data = json.loads(row["assessment_json"])
						data["suggested_label_ids"] = [item for item in data["suggested_label_ids"] if item not in (label_ids or [])]
						self.store.execute("UPDATE predictions SET assessment_json=? WHERE id=?", (encode(data), row["id"]))
					continue
				decision = {"mistaken_archive": "keep", "unnecessary_keep": "archive"}.get(action, action)
				self.store.execute("UPDATE messages SET decision=? WHERE id=?", (decision, mid))
				if decision in {"archive", "label"}:
					aid = self.actions.plan("", message, decision, label_ids or [], reason or "Explicit review", "manual", window, bypass)
					if aid:
						result.append(aid)
		if undo_ids and not should_stop():
			self.undo(undo_ids, should_stop=should_stop)
			marks = ",".join("?" for _ in undo_ids)
			result.extend(row["id"] for row in self.store.query(f"SELECT id FROM actions WHERE undo_of IN ({marks})", undo_ids))
		if result and not self.config.dry_run and not should_stop():
			self.apply_actions(result, should_stop=should_stop)
		return result

	def plan_snapshot(self, action_ids=None, job_id=None):
		return self.actions.plan_snapshot(action_ids, job_id)

	def apply_actions(self, action_ids=None, job_id=None, should_stop=None, *, expected_plan_hash=None, allow_over_limit=False, expected_dry_run=None):
		with self.execution_lease():
			self.reload_config()
			return self.actions.apply(action_ids, job_id, should_stop=self._stop_callback(should_stop), expected_plan_hash=expected_plan_hash, allow_over_limit=allow_over_limit or self._session_write_confirmation, expected_dry_run=expected_dry_run)

	def undo(self, action_ids, should_stop=None, *, allow_unverified=False, confirmed=False, allow_over_limit=False):
		with self.execution_lease():
			return self.actions.undo(action_ids, should_stop=self._stop_callback(should_stop), allow_unverified=allow_unverified, confirmed=confirmed, allow_over_limit=allow_over_limit or self._session_write_confirmation)

	def replan(self, should_stop=None):
		should_stop = self._stop_callback(should_stop)
		self.reload_config()
		count = 0
		# Keep each original job's frozen target bounds. Replanning never restores
		# earlier archives; only current inbox items receive new proposals.
		rows = self.store.query("SELECT p.* FROM predictions p JOIN messages m ON p.message_id=m.id WHERE p.valid=1 AND m.in_inbox=1 ORDER BY p.created_at DESC")
		seen = set()
		for row in rows:
			if should_stop():
				break
			if row["message_id"] in seen:
				continue
			seen.add(row["message_id"])
			job = self.store.one("SELECT window_json,config_json FROM jobs WHERE id=?", (row["job_id"],))
			window = Window.from_dict(json.loads(job["window_json"])) if job else resolve_window(self.config, self.clock())
			message = self.store.get_message(row["message_id"])
			if not self.eligible(message, window):
				continue
			assessment = Assessment.from_dict(json.loads(row["assessment_json"]))
			frozen = Config.from_dict(json.loads(job["config_json"])) if job else self.config
			effective = self.jobs.effective_config(frozen)
			override = self.override(message)
			decision = recommend(message, assessment, effective, window, override, self.rules())
			self.jobs.save_recommendation(row["job_id"], message, assessment, decision.action, decision.reason, window, row["model_version"], label_ids=decision.label_ids, approval="manual" if override and decision.evidence_source == "manual" else "automatic")
			count += 1
		return {"replanned": count, "ai_calls": 0}

	def connect(self, credentials_path):
		if self.demo:
			raise ValueError("Demo cannot authenticate to Gmail; launch Personal mode")
		from .auth import AuthManager
		previous_store = self.store
		previous_account = self.account_id
		with self.execution_lease():
			mailbox = AuthManager(self.data_dir, retrieval_batch_size=self.config.retrieval_batch_size, execution_batch_size=self.config.execution_batch_size, body_limit=self.config.body_limit).connect(credentials_path)
			identity = mailbox.identity().casefold()
			new_store = Store(self._database(identity), identity)
			saved = new_store.checkpoint("effective_preferences")
			base = Config.from_dict(saved or previous_store.checkpoint("effective_preferences", Config().to_dict()))
			if not saved and previous_account not in {"unconnected", identity}:
				# User-label IDs have account-local identity. Carrying their meanings
				# to a different account could protect or select an unrelated label.
				base.label_meanings = {}
				base.protected_label_ids = []
				base.excluded_label_ids = []
			new_store.set_checkpoint("effective_preferences", base.to_dict())
			self.config = Config.from_dict({**base.to_dict(), **self._config_overrides})
			self._close_ai()
			self.account_id = identity
			self._mailbox = mailbox
			self._supplied_mailbox = False
			self._write_identity()
		previous_store.close()
		self.store = new_store
		self._configure_services()
		self.refresh_labels()
		return {"account_id": identity}

	def disconnect(self, revoke=False):
		if self.demo:
			return {"message": "Demo has no OAuth credentials"}
		from .auth import AuthManager
		with self.execution_lease():
			AuthManager(self.data_dir).disconnect(revoke=revoke)
			self._write_identity(connected=False)
			self._mailbox = None
			self._close_ai()
		return {"message": "Disconnected and Google consent revoked; local data and undo history retained" if revoke else "Disconnected; local data and undo history retained"}

	def error(self, stage, exc, job_id="", message_id=""):
		insert(self.store, "errors", {"id": identifier(), "job_id": job_id, "message_id": message_id, "stage": stage, "kind": getattr(exc, "category", getattr(exc, "kind", type(exc).__name__)), "class_name": getattr(exc, "exception_class", type(exc).__name__), "summary": safe_error(exc), "created_at": stamp(), "payload_json": encode({"exception_module": getattr(exc, "exception_module", type(exc).__module__)})})

	def retry_errors(self, *, job_id=None, work_item_ids=None):
		"""Reset explicitly scoped work without reviving a user-paused job."""
		selection = sorted(set(work_item_ids or []))
		if not job_id and not selection:
			raise ValueError("Choose a job or work-item selection to retry")
		with self.store.transaction():
			rows = self.store.query("SELECT id,job_id,message_id,stage FROM work_items WHERE state IN ('failed','retry') "
				"AND (? IS NULL OR job_id=?) AND (?='[]' OR id IN (SELECT value FROM json_each(?)))",
				(job_id, job_id, encode(selection), encode(selection)))
			jobs = {row["job_id"] for row in rows}
			for row in rows:
				self.store.execute("UPDATE work_items SET state='pending',attempts=0,next_attempt_utc=NULL,error='' WHERE id=?", (row["id"],))
				self.store.execute("UPDATE errors SET state='retry_requested' WHERE state='open' AND job_id=? AND message_id=? AND stage=?", (row["job_id"], row["message_id"], row["stage"]))
			for job in jobs:
				current = self.store.one("SELECT state,payload_json FROM jobs WHERE id=?", (job,))
				payload = json.loads(current["payload_json"])
				pause = payload.get("pause", {})
				if current["state"] == "stopped" or (current["state"] == "paused" and pause.get("origin") != "system"):
					continue
				for key in ("pause", "backoff", "system_retries"):
					payload.pop(key, None)
				self.jobs.save_payload(job, payload)
				self.store.execute("UPDATE jobs SET state='pending' WHERE id=?", (job,))
		return {"queued": len(rows), "jobs": len(jobs), "message": "Selected failed work queued with fresh attempts; user-paused jobs remain paused. Unknown writes reconcile before retry."}

	def retention(self):
		before = stamp(self.clock() - timedelta(days=self.config.retention_days))
		return {"bodies_removed": self.store.prune_cache(before), "rows_removed": self.store.housekeeping(before)}

	def backup(self, path):
		return str(self.store.backup(path))

	def _close_ai(self, grace_s=2):
		with self._clients_lock:
			clients = list(self._clients.values())
			self._clients.clear()
		for client in clients:
			if isinstance(client, AIClient):
				client.close(grace_s=grace_s)
			else:
				# Injected offline test adapters need only the original close method.
				client.close()

	def close(self):
		if self._closed:
			return
		self._closed = True
		try:
			if self._resource_owner is self:
				self.abort_workers()
				self._close_ai(grace_s=0.1)
		finally:
			self.store.close()
