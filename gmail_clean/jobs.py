"""Durable discovery and assessment stages, independent of Qt and Gmail transport."""

from __future__ import annotations

import contextlib
import json
import random
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from .ai import PROMPT_VERSION
from .domain import Assessment, Config, Window, content_excluded, content_hash, context_hash, epoch_ms, recommend, resolve_age_cutoff, resolve_window
from .runtime import chunks, placeholders, digest, encode, identifier, insert, safe_error, stamp

if TYPE_CHECKING:
	from .service import AppService


class JobRunner:
	def __init__(self, service: AppService):
		self.s = service
		self.db = service.store

	def new(self) -> str:
		config = self.s.config
		config.validate()
		anchor = self.s.clock()
		window = resolve_window(config, anchor)
		job_id = identifier()
		insert(self.db, "jobs", {
			"id": job_id, "account_id": self.s.account_id, "state": "pending", "stage": "discovery",
			"anchor_utc": stamp(anchor), "window_json": encode(window.to_dict()),
			"config_json": encode(config.to_dict()), "created_at": stamp(), "updated_at": stamp(),
			"payload_json": "{}",
		})
		return job_id

	def payload(self, job: str) -> dict:
		return json.loads(self.db.one("SELECT payload_json FROM jobs WHERE id=?", (job,))["payload_json"])

	def save_payload(self, job: str, payload: dict) -> None:
		self.db.execute("UPDATE jobs SET payload_json=?,updated_at=? WHERE id=?", (encode(payload), stamp(), job))

	def checkpoint(self, key: str, default=None):
		row = self.db.one("SELECT value_json FROM checkpoints WHERE key=?", (key,))
		return json.loads(row["value_json"]) if row else default

	def set_checkpoint(self, key: str, value) -> None:
		insert(self.db, "checkpoints", {"key": key, "value_json": encode(value), "updated_at": stamp()}, replace=True)

	def queue(self, job: str, message_id: str, stage: str = "metadata") -> None:
		self.db.execute(
			"INSERT OR IGNORE INTO work_items(job_id,message_id,stage,state) VALUES(?,?,?,'pending')",
			(job, message_id, stage),
		)

	def held(self, job: str, should_stop: Callable | None) -> bool:
		row = self.db.one("SELECT state FROM jobs WHERE id=?", (job,))
		if row["state"] in {"paused", "stopped"}:
			return True
		backoff = self.payload(job).get("backoff", {})
		if row["state"] == "waiting" and backoff.get("due") and datetime.fromisoformat(backoff["due"]) > self.s.clock():
			return True
		signal = should_stop() if callable(should_stop) else False
		if signal:
			state = signal if signal in {"paused", "stopped"} else "paused"
			payload = self.payload(job)
			payload["pause"] = {"origin": "user", "kind": state, "reason": "Paused by the user", "due": None}
			self.save_payload(job, payload)
			self.db.execute("UPDATE jobs SET state=?,updated_at=? WHERE id=?", (state, stamp(), job))
			return True
		return False

	def rules_revision(self) -> str:
		"""Track effective rule edits, not changing support counts or timestamps."""
		values = [{key: rule.get(key) for key in ("id", "enabled", "conditions", "action", "label_ids", "source", "revision")} for rule in self.s.rules()]
		return digest(sorted(values, key=encode))

	def example_signature(self, examples: list[dict]) -> list[dict]:
		"""Only selected prompt inputs affect reuse; statistics are not model inputs."""
		keys = ("id", "message_id", "sender", "category", "action", "reason", "scope", "correction", "context_version", "version", "label_ids", "negative_label_ids", "subject")
		result = []
		for example in examples:
			value = {key: example[key] for key in keys if key in example}
			if isinstance(example.get("body"), str):
				value["body"] = example["body"][:1600]
			result.append(value)
		return result

	def queue_local_transitions(self, job: str, window: Window, config: Config, *, full: bool) -> None:
		"""Revisit quiet messages only when local time or semantic inputs need it.

		History records mailbox changes, not the passage of time. Keep a durable
		last-completed anchor so a message crossing an age/defer/deadline boundary is
		found without repeatedly fetching the entire unchanged inbox from Gmail.
		"""
		payload = self.payload(job)
		policy = payload.setdefault("policy_revision", self.s.config.revision)
		rules = payload.setdefault("rules_revision", self.rules_revision())
		prompt = payload.setdefault("prompt_version", PROMPT_VERSION)
		self.save_payload(job, payload)
		force = (full or self.checkpoint("boundary_policy_revision") != policy
			or self.checkpoint("boundary_rules_revision") != rules
			or self.checkpoint("boundary_prompt_version") != prompt)
		previous = self.checkpoint("boundary_anchor")
		previous_anchor = datetime.fromisoformat(previous) if previous else None
		effective = self.effective_config(config)
		rows = self.db.query("""SELECT m.id,m.internal_ms,m.category,m.decision,m.defer_until_utc,m.deadline_utc,
			EXISTS(SELECT 1 FROM predictions p WHERE p.message_id=m.id AND p.valid=1
				AND p.content_hash=m.content_hash AND p.context_version=m.context_version
				AND p.prompt_version=?) AS has_assessment
			FROM messages m WHERE m.in_inbox=1 AND NOT EXISTS
				(SELECT 1 FROM work_items w WHERE w.job_id=? AND w.message_id=m.id AND w.stage='metadata')""", (PROMPT_VERSION, job))
		with self.db.transaction():
			for row in rows:
				if not window.contains(row["internal_ms"]):
					continue
				needs = force or not row["decision"] or row["decision"] == "pending" or not row["has_assessment"]
				for field in ("defer_until_utc", "deadline_utc"):
					if row[field]:
						try:
							due = datetime.fromisoformat(row[field].replace("Z", "+00:00"))
							needs = needs or (due <= window.anchor_utc and (previous_anchor is None or previous_anchor < due))
						except (TypeError, ValueError):
							# Malformed state must be reconsidered, never silently retained.
							needs = True
				if row["decision"] == "review" and not needs:
					try:
						cutoff = resolve_age_cutoff(effective, window.anchor_utc, row["category"])
						old_cutoff = resolve_age_cutoff(effective, previous_anchor, row["category"]) if previous_anchor else None
						needs = cutoff is not None and row["internal_ms"] < epoch_ms(cutoff) and (old_cutoff is None or row["internal_ms"] >= epoch_ms(old_cutoff))
					except (TypeError, ValueError):
						needs = True
				if needs:
					self.queue(job, row["id"])

	def effective_config(self, frozen: Config) -> Config:
		"""Recompute current policy without broadening an older job's protections.

		The job's date interval remains frozen separately. Removing a protection or
		content exclusion applies to a new job; adding one takes effect immediately.
		"""
		current = Config.from_dict(self.s.config.to_dict())
		for name in ("protected_senders", "protected_domains", "protected_label_ids", "excluded_senders", "excluded_domains", "excluded_label_ids", "excluded_categories"):
			setattr(current, name, sorted(set(getattr(current, name)) | set(getattr(frozen, name))))
		current.protect_starred = current.protect_starred or frozen.protect_starred
		current.sent_context_enabled = current.sent_context_enabled and frozen.sent_context_enabled
		current.archived_context_enabled = current.archived_context_enabled and frozen.archived_context_enabled
		current.body_limit = min(current.body_limit, frozen.body_limit)
		for label_id, meaning in frozen.label_meanings.items():
			if meaning in {"protected", "attention", "requires_attention", "requires attention"}:
				current.label_meanings[label_id] = meaning
		return current

	def excluded(self, message, config: Config) -> bool:
		if content_excluded(message, config) or self.s.excluded(message):
			return True
		row = self.db.one("SELECT category FROM messages WHERE id=?", (message.id,))
		return bool(row and row["category"] in config.excluded_categories)

	def examples(self, message, config: Config, window: Window) -> list[dict]:
		"""Historical examples share the same date/privacy limits as target text."""
		if not config.learning_enabled:
			return []
		current_window = resolve_window(self.s.config, window.anchor_utc)
		result = []
		for example in self.s.examples(message):
			source = self.db.get_message(example.get("message_id", ""))
			if not source or not window.contains(source.internal_ms) or not current_window.contains(source.internal_ms):
				continue
			if self.excluded(source, config) or example.get("category") in config.excluded_categories:
				continue
			result.append(example)
		return result

	def queue_body(self, job: str, message_id: str, *, download_only: bool = False) -> None:
		self.queue(job, message_id, "body")
		self.db.execute("UPDATE work_items SET payload_json=? WHERE job_id=? AND message_id=? AND stage='body'", (encode({"download_only": download_only}), job, message_id))

	def run(self, job: str, progress: Callable | None = None, should_stop: Callable | None = None) -> dict:
		row = self.db.one("SELECT * FROM jobs WHERE id=?", (job,))
		if not row or row["account_id"] != self.s.account_id:
			raise ValueError("Job belongs to a different account or does not exist")
		backoff = json.loads(row["payload_json"]).get("backoff", {})
		if row["state"] == "waiting" and backoff.get("due") and datetime.fromisoformat(backoff["due"]) > self.s.clock():
			return self.summary(job)
		payload = json.loads(row["payload_json"])
		payload.pop("pause", None)
		self.save_payload(job, payload)
		window = Window.from_dict(json.loads(row["window_json"]))
		config = Config.from_dict(json.loads(row["config_json"]))
		# Recovered running items were never acknowledged; stages are idempotent.
		self.db.execute("UPDATE work_items SET state='pending' WHERE job_id=? AND state='running'", (job,))
		self.db.execute("UPDATE jobs SET state='running',updated_at=? WHERE id=?", (stamp(), job))
		try:
			if not self.payload(job).get("discovered"):
				if not self.discover(job, window, config, progress, should_stop):
					return self.summary(job)
			for stage in ("metadata", "body", "classification"):
				self.db.execute("UPDATE jobs SET stage=?,updated_at=? WHERE id=?", (stage, stamp(), job))
				if not self.process_stage(job, stage, window, config, progress, should_stop):
					return self.summary(job)
			payload = self.payload(job)
			for key in ("pause", "backoff", "system_retries"):
				payload.pop(key, None)
			self.save_payload(job, payload)
			self.db.execute("UPDATE jobs SET stage='review',state='completed',updated_at=? WHERE id=?", (stamp(), job))
			self.set_checkpoint("boundary_latest", window.to_dict()["latest_exclusive"])
			self.set_checkpoint("boundary_window", {"earliest_date": config.earliest_date, "latest_mode": config.latest_mode, "latest_days": config.latest_days, "latest_date": config.latest_date, "timezone": config.timezone})
			completed_inputs = self.payload(job)
			self.set_checkpoint("boundary_anchor", stamp(window.anchor_utc))
			self.set_checkpoint("boundary_policy_revision", completed_inputs.get("policy_revision", config.revision))
			self.set_checkpoint("boundary_rules_revision", completed_inputs.get("rules_revision", self.rules_revision()))
			self.set_checkpoint("boundary_prompt_version", completed_inputs.get("prompt_version", PROMPT_VERSION))
			if self.s.config.auto_execute and not self.s.config.dry_run:
				self.s.apply_actions(job_id=job, should_stop=should_stop)
		except Exception as exc:  # noqa: BLE001 - job boundary persists a kind-aware resumable pause
			self.s.error("job", exc, job_id=job)
			self.handle_exception(job, exc, config)
			if progress:
				progress("Paused: " + safe_error(exc))
		return self.summary(job)

	def discover(self, job: str, window: Window, config: Config, progress, should_stop) -> bool:
		self.db.execute("UPDATE jobs SET stage='discovery' WHERE id=?", (job,))
		payload = self.payload(job)
		if "initial_history" not in payload:
			payload["initial_history"] = str(self.s.mailbox.profile()["historyId"])
			self.save_payload(job, payload)
		start = self.checkpoint("history_id")
		full = start is None
		payload.setdefault("refresh_all", start is None)
		if not payload.get("history_done") and start:
			try:
				if not self.sync_history(job, start, should_stop):
					return False
			except Exception as exc:
				from .gmail import HistoryExpired
				if not isinstance(exc, HistoryExpired):
					raise
				full = True
				payload["reconciliation"] = True
				payload["refresh_all"] = True
			payload["history_done"] = True
			self.save_payload(job, payload)
		last_full = self.checkpoint("full_reconciliation")
		if not last_full or (window.anchor_utc - datetime.fromisoformat(last_full)).total_seconds() >= config.reconciliation_interval_hours * 3600:
			full = True
		full = full or payload.get("reconciliation", False)
		# A loosened lower bound or changed calendar mode cannot be found by a
		# rolling upper-bound sweep alone. Safely re-enumerate and deduplicate.
		previous_window = self.checkpoint("boundary_window")
		scope = {"earliest_date": config.earliest_date, "latest_mode": config.latest_mode, "latest_days": config.latest_days, "latest_date": config.latest_date, "timezone": config.timezone}
		if previous_window is not None and previous_window != scope:
			full = True
		if "query" not in payload:
			previous = self.checkpoint("boundary_latest")
			if full or not previous or config.latest_mode != "rolling":
				payload["query"] = window.query()
				payload["full"] = True
			else:
				low = int(datetime.fromisoformat(previous).timestamp()) - 86400
				high = int(window.latest_exclusive.timestamp()) + 86400
				payload["query"] = f"label:INBOX -in:spam -in:trash -is:draft after:{low} before:{high}"
			payload["page_token"] = None
			self.save_payload(job, payload)
		if not payload.get("enumerated"):
			local_inbox = {row["id"] for row in self.db.query("SELECT id FROM messages WHERE in_inbox=1")}
			restarts = 0
			while True:
				if self.held(job, should_stop):
					return False
				self.s.heartbeat(job)
				try:
					page = self.s.mailbox.list_messages(payload["query"], payload.get("page_token"), page_size=500)
				except Exception as exc:
					from .gmail import PageTokenExpired
					if isinstance(exc, PageTokenExpired) and restarts < config.max_retries:
						payload["page_token"] = None
						self.save_payload(job, payload)
						restarts += 1
						continue
					raise
				with self.db.transaction():
					for item in page.get("messages", []):
						if payload.get("refresh_all") or item["id"] not in local_inbox:
							self.queue(job, item["id"])
					payload["page_token"] = page.get("nextPageToken")
					payload["enumerated"] = not bool(payload["page_token"])
					self.save_payload(job, payload)
				if progress:
					count = self.db.one("SELECT count(*) n FROM work_items WHERE job_id=?", (job,))["n"]
					progress(f"Discovery: {count:,} durable candidate IDs")
				if payload["enumerated"]:
					break
		# Reconcile arrivals/removals DURING non-atomic enumeration before any mutations.
		history_valid = True
		try:
			if not self.sync_history(job, payload["initial_history"], should_stop):
				return False
		except Exception as exc:
			from .gmail import HistoryExpired
			if not isinstance(exc, HistoryExpired):
				raise
			# Clear both boundaries so a valid later checkpoint cannot incorrectly
			# authorize the new-ID-only optimization across this uncovered interval.
			history_valid = False
			self.set_checkpoint("history_id", None)
			self.set_checkpoint("full_reconciliation", None)
		# Quiet time-driven transitions still need work, but an unchanged decided
		# inbox does not need a complete Gmail metadata refresh on every cycle.
		self.queue_local_transitions(job, window, config, full=bool(payload.get("refresh_all")))
		payload = self.payload(job)
		if payload.get("full") and history_valid:
			# Full enumeration completes only with a covered history interval.
			self.set_checkpoint("full_reconciliation", stamp(window.anchor_utc))
		payload["discovered"] = True
		self.save_payload(job, payload)
		return True

	def sync_history(self, job: str, start: str, should_stop=None) -> bool:
		key = "history_page:" + str(start)
		payload = self.payload(job)
		token = payload.get(key)
		restarts = 0
		while True:
			if self.held(job, should_stop):
				return False
			try:
				page = self.s.mailbox.history(str(start), token)
			except Exception as exc:
				from .gmail import PageTokenExpired
				if isinstance(exc, PageTokenExpired) and restarts < self.s.config.max_retries:
					# The start history ID remains the durable replay boundary; only
					# the invalid pagination cursor is discarded. IDs deduplicate.
					token = None
					payload[key] = None
					self.save_payload(job, payload)
					restarts += 1
					continue
				raise
			with self.db.transaction():
				for event in page.get("history", []):
					# Specific event arrays avoid duplicating generic 'messages' entries.
					for kind in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
						for change in event.get(kind, []):
							message_id = change.get("message", {}).get("id")
							if message_id:
								self.queue(job, message_id)
				payload[key] = page.get("nextPageToken")
				self.save_payload(job, payload)
				if not payload[key]:
					self.set_checkpoint("history_id", str(page.get("historyId", start)))
			if not payload[key]:
				return True
			token = payload[key]


	def _prefetch_threads(self, messages) -> dict:
		"""Fetch conversation evidence before taking SQLite's writer lock.

		Cache failures as well as successful results for this phase: every row in
		a failed conversation gets its own retry without repeating the request.
		"""
		threads = {}
		for message in messages:
			if isinstance(message, Exception) or message.thread_id in threads:
				continue
			try:
				threads[message.thread_id] = self.s.mailbox.thread_metadata(message.thread_id)
			except Exception as exc:  # noqa: BLE001 - thread transport failures are returned as per-thread evidence
				threads[message.thread_id] = exc
		return threads

	def _thread_members(self, message, threads):
		if message.thread_id not in threads:
			if self.db.conn.in_transaction:
				raise RuntimeError("Conversation evidence must be fetched before a write transaction")
			threads.update(self._prefetch_threads([message]))
		members = threads[message.thread_id]
		if isinstance(members, Exception):
			raise members
		return members

	def process_stage(self, job: str, stage: str, window: Window, config: Config, progress, should_stop) -> bool:
		while True:
			if self.held(job, should_stop):
				return False
			self.s.reload_config()
			effective = self.effective_config(config)
			batch_size = effective.retrieval_batch_size if stage != "classification" else effective.ai_batch_size
			batch_size = min(batch_size, self.payload(job).get("batch_sizes", {}).get(stage, batch_size))
			rows = self.db.query(
				"SELECT * FROM work_items WHERE job_id=? AND stage=? AND state IN ('pending','retry') "
				"AND (next_attempt_utc IS NULL OR next_attempt_utc<=?) ORDER BY message_id LIMIT ?",
				(job, stage, stamp(self.s.clock()), batch_size),
			)
			if not rows:
				waiting = self.db.one("SELECT count(*) n FROM work_items WHERE job_id=? AND stage=? AND state='retry'", (job, stage))["n"]
				if waiting:
					self.db.execute("UPDATE jobs SET state='waiting' WHERE id=?", (job,))
					return False
				return True
			self.s.heartbeat(job)
			batch_policy = self.s.policy_batch([row["message_id"] for row in rows]) if hasattr(self.s, "policy_batch") else contextlib.nullcontext()
			with batch_policy:
				if stage == "body":
					# A paused body queue may predate stricter settings or a manual keep.
					# Recheck before requesting any content, not after downloading it.
					try:
						live_metadata = self.s.mailbox.get_messages([row["message_id"] for row in rows], body=False)
					except Exception as exc:  # noqa: BLE001 - metadata transport failure is assigned to unresolved work only
						live_metadata = {row["message_id"]: exc for row in rows}
					allowed = []
					threads = self._prefetch_threads(
						message for row in rows
						if not isinstance(message := live_metadata.get(row["message_id"], ValueError("Missing body-preflight metadata")), Exception)
						and self.s.eligible(message, window) and not self.excluded(message, effective)
						and (not json.loads(row["payload_json"]).get("download_only", False) or effective.download_all_bodies)
					)
					with self.db.transaction():
						for row in rows:
							old = self.db.get_message(row["message_id"])
							message = live_metadata.get(row["message_id"], ValueError("Missing body-preflight metadata"))
							if isinstance(message, Exception):
								self.fail(job, row, message, effective)
								continue
							if old and old.headers == message.headers and old.internal_ms == message.internal_ms:
								message.body, message.truncated, message.missing_content = old.body, old.truncated, old.missing_content
								message.context_version = old.context_version
								message.content_hash = old.content_hash
							self.db.upsert_message(message)
							if not self.s.eligible(message, window):
								self.db.execute("UPDATE messages SET decision='skip' WHERE id=?", (message.id,))
								self.db.execute("UPDATE actions SET state='stale',reason='No longer an eligible inbox target' WHERE message_id=? AND state IN ('queued','proposed')", (message.id,))
								self.done(job, row["message_id"], stage)
								continue
							if self.excluded(message, effective):
								self.save_recommendation(job, message, None, "review", "Content sharing excluded by effective privacy settings", window)
								self.done(job, message.id, stage)
								continue
							download_only = json.loads(row["payload_json"]).get("download_only", False)
							if download_only and not effective.download_all_bodies:
								self.done(job, message.id, stage)
								continue
							try:
								members = self._thread_members(message, threads)
								message.context_version = context_hash(members, effective.sent_context_enabled)
								current_window = resolve_window(self.s.config, window.anchor_utc)
								newer_excluded = any(member.id != message.id and member.internal_ms > message.internal_ms and (not window.contains(member.internal_ms) or not current_window.contains(member.internal_ms)) and not ({"TRASH", "SPAM", "DRAFT"} & member.labels) for member in members)
								context_changed = bool(old and old.context_version != message.context_version)
								self.db.upsert_message(message)
								if context_changed:
									self.db.execute("UPDATE predictions SET valid=0 WHERE message_id=?", (message.id,))
									self.db.execute("UPDATE actions SET state='stale',reason='Conversation changed during body queue' WHERE message_id=? AND state IN ('queued','proposed')", (message.id,))
									self.db.execute("DELETE FROM manual_overrides WHERE message_id=?", (message.id,))
									self.db.execute("UPDATE messages SET defer_until_utc=NULL,deadline_utc=NULL WHERE id=?", (message.id,))
								if context_changed or newer_excluded:
									self.set_checkpoint(f"recent_reply:{message.id}", newer_excluded)
									self.save_recommendation(job, message, None, "review", "Conversation changed or has a newer excluded reply; reconsider before classification", window)
									if effective.download_all_bodies:
										row["payload_json"] = encode({"download_only": True})
										self.db.execute("UPDATE work_items SET payload_json=? WHERE id=?", (row["payload_json"], row["id"]))
										allowed.append(row)
									else:
										self.done(job, message.id, stage)
									continue
							except Exception as exc:  # noqa: BLE001 - per-item safety check failure enters the kind-aware work queue
								self.fail(job, row, exc, effective)
								continue
							direct = recommend(message, None, effective, window, manual_override=self.s.override(message), rules=self.s.rules())
							if direct.action in {"keep", "skip", "archive"}:
								self.save_recommendation(job, message, None, direct.action, direct.reason, window, label_ids=direct.label_ids, approval="manual" if direct.evidence_source == "manual" else "automatic")
								if effective.download_all_bodies:
									row["payload_json"] = encode({"download_only": True})
									self.db.execute("UPDATE work_items SET payload_json=? WHERE id=?", (row["payload_json"], row["id"]))
								else:
									self.done(job, message.id, stage)
									continue
							allowed.append(row)
					rows = allowed
					if not rows:
						continue
				ids = [r["message_id"] for r in rows]
				for selected in chunks(ids):
					self.db.execute(
						f"UPDATE work_items SET state='running' WHERE job_id=? AND stage=? AND message_id IN ({placeholders(selected)})",  # noqa: S608 -- chunk contains only bind markers; job, stage and IDs stay bound
						(job, stage, *selected),
					)
				if stage in {"metadata", "body"}:
					try:
						results = self.s.mailbox.get_messages(ids, body=stage == "body")
					except Exception as exc:  # noqa: BLE001 - multipart failure produces independent per-item error results
						results = {mid: exc for mid in ids}
					threads = self._prefetch_threads(
						message for message in results.values()
						if not isinstance(message, Exception) and self.s.eligible(message, window)
					) if stage == "metadata" else {}
					with self.db.transaction():
						for row in rows:
							mid = row["message_id"]
							result = results.get(mid, ValueError("Missing retrieval result"))
							try:
								with self.db.transaction():
									if isinstance(result, Exception):
										raise result
									if stage == "metadata":
										self.metadata(job, result, window, effective, threads=threads)
									else:
										# Eligibility is established before this retrieval was queued.
										if self.s.eligible(result, window):
											old = self.db.get_message(mid)
											result.context_version = old.context_version if old else ""
											if len(result.body or "") > effective.body_limit:
												result.body = result.body[:effective.body_limit]
												result.truncated = True
											result.content_hash = content_hash(result)
											self.db.upsert_message(result)
											if not json.loads(row["payload_json"]).get("download_only", False):
												self.queue(job, mid, "classification")
									self.done(job, mid, stage)
							except Exception as exc:  # noqa: BLE001 - one persistence failure must not discard prior completed rows
								self.fail(job, row, exc, effective)
				else:
					self.classify(job, rows, window, effective)
			if progress:
				counts = self.summary(job)
				progress(f"{stage.title()}: {counts['completed']:,} stages completed; {counts['failed']:,} errors")

	def metadata(self, job: str, message, window: Window, config: Config, *, threads=None) -> None:
		old = self.db.get_message(message.id)
		if not self.s.eligible(message, window):
			# Out-of-window bodies are never fetched or sent to AI.
			message.body = ""
			self.db.upsert_message(message)
			self.db.execute("UPDATE messages SET decision='skip' WHERE id=?", (message.id,))
			return
		threads = {} if threads is None else threads
		members = self._thread_members(message, threads)
		message.context_version = context_hash(members, config.sent_context_enabled)
		if old and old.internal_ms == message.internal_ms and old.headers == message.headers:
			message.body, message.truncated, message.missing_content = old.body, old.truncated, old.missing_content
		if len(message.body or "") > config.body_limit:
			message.body = message.body[:config.body_limit]
			message.truncated = True
		message.content_hash = content_hash(message)
		self.db.upsert_message(message)
		current_window = resolve_window(self.s.config, window.anchor_utc)
		newer_excluded = any(m.id != message.id and m.internal_ms > message.internal_ms and (not window.contains(m.internal_ms) or not current_window.contains(m.internal_ms)) and not ({"TRASH", "SPAM", "DRAFT"} & set(m.labels)) for m in members)
		self.set_checkpoint(f"recent_reply:{message.id}", newer_excluded)
		if old and old.context_version != message.context_version:
			self.db.execute("UPDATE predictions SET valid=0 WHERE message_id=?", (message.id,))
			self.db.execute("UPDATE actions SET state='stale',reason='Conversation changed' WHERE message_id=? AND state IN ('queued','proposed')", (message.id,))
			self.db.execute("DELETE FROM manual_overrides WHERE message_id=?", (message.id,))
			self.db.execute("UPDATE messages SET defer_until_utc=NULL,deadline_utc=NULL WHERE id=?", (message.id,))
		if newer_excluded:
			self.save_recommendation(job, message, None, "review", "Newer excluded reply makes earlier conclusions uncertain", window)
			if config.download_all_bodies and not message.body and not self.excluded(message, config):
				self.queue_body(job, message.id, download_only=True)
			return
		override = self.s.override(message)
		direct = recommend(message, None, config, window, manual_override=override, rules=self.s.rules())
		if direct.action in {"keep", "skip", "archive"}:
			self.save_recommendation(job, message, None, direct.action, direct.reason, window, label_ids=direct.label_ids, approval="manual" if direct.evidence_source == "manual" else "automatic")
			if config.download_all_bodies and not message.body and not self.excluded(message, config):
				self.queue_body(job, message.id, download_only=True)
			return
		if self.excluded(message, config):
			self.save_recommendation(job, message, None, "review", "Content sharing excluded by current privacy settings", window)
			return
		if direct.label_ids:
			# Enabled label-only rules are deterministic policies. Their additions
			# remain independent of the unresolved disposition assessment.
			self.save_recommendation(job, message, None, "review", direct.reason, window, label_ids=direct.label_ids)
		if message.body:
			self.queue(job, message.id, "classification")
		else:
			self.queue_body(job, message.id)

	def route_category(self, message, config: Config) -> str:
		"""A routing hint selects only an explicitly configured category model.

		Gmail's broad category labels and List-ID are hints for model routing, not
		proof that a message is safe to archive or a replacement for assessment.
		"""
		row = self.db.one("SELECT category FROM messages WHERE id=?", (message.id,))
		category = row["category"] if row else ""
		if category not in config.optional_model_routes:
			category = ""
			if "CATEGORY_PROMOTIONS" in message.labels:
				category = "promotion"
			elif "CATEGORY_SOCIAL" in message.labels:
				category = "notification"
			elif message.header("List-ID"):
				category = "newsletter"
		return category if category in config.optional_model_routes else ""

	def reserve_ai_call(self, job: str, config: Config) -> bool:
		"""Reserve estimated spend durably before a potentially billable request.

		A crashed or failed call still counts: absence of a response does not prove
		that the provider avoided billing it. Daily limits use explicit UTC days.
		"""
		now = self.s.clock().astimezone(UTC)
		day_key = "ai_budget_daily:" + now.date().isoformat()
		cost = config.estimated_cost_per_call
		with self.db.transaction():
			payload = self.payload(job)
			usage = payload.get("ai_usage", {"calls": 0, "estimated_cost": 0.0})
			daily = self.checkpoint(day_key, {"calls": 0, "estimated_cost": 0.0})
			if (config.max_ai_cost_per_job or config.max_ai_cost_per_day) and cost <= 0:
				self.pause_system(job, "ai_budget_configuration", "Set a positive estimated cost per call before using an AI cost cap")
				return False
			for scope, counts, calls_cap, cost_cap in (
				("job", usage, config.max_ai_calls_per_job, config.max_ai_cost_per_job),
				("daily", daily, config.max_ai_calls_per_day, config.max_ai_cost_per_day),
			):
				if ((calls_cap and counts["calls"] + 1 > calls_cap)
					or (cost_cap and counts["estimated_cost"] + cost > cost_cap + 1e-9)):
					due = stamp((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)) if scope == "daily" else None
					self.pause_system(job, "ai_budget_" + scope, f"AI {scope} spending cap reached; reserved {counts['calls']} calls and ${counts['estimated_cost']:.4f} estimated cost. Raise the cap explicitly or wait for the daily reset.", due=due)
					return False
			for counts in (usage, daily):
				counts["calls"] += 1
				counts["estimated_cost"] = round(counts["estimated_cost"] + cost, 10)
			payload["ai_usage"] = usage
			self.save_payload(job, payload)
			self.set_checkpoint(day_key, daily)
		return True

	def classify(self, job: str, rows: list[dict], window: Window, config: Config) -> None:
		pending = defaultdict(list)
		try:
			live_metadata = self.s.mailbox.get_messages([row["message_id"] for row in rows], body=False)
		except Exception as exc:  # noqa: BLE001 - classification metadata transport failure remains per-item evidence
			live_metadata = {row["message_id"]: exc for row in rows}
		threads = {}
		for row in rows:
			stored = self.db.get_message(row["message_id"])
			message = live_metadata.get(row["message_id"], ValueError("Missing classification-preflight metadata"))
			if isinstance(message, Exception):
				self.fail(job, row, message, config)
				continue
			if stored and stored.internal_ms == message.internal_ms and stored.headers == message.headers:
				message.body, message.truncated, message.missing_content = stored.body, stored.truncated, stored.missing_content
				message.context_version, message.content_hash = stored.context_version, stored.content_hash
			self.db.upsert_message(message)
			if not stored or not self.s.eligible(message, window) or self.excluded(message, config):
				if stored:
					self.save_recommendation(job, message, None, "review" if self.s.eligible(message, window) else "skip", "Classification preflight found changed scope or content-sharing state", window)
				self.done(job, row["message_id"], "classification")
				continue
			try:
				if message.thread_id not in threads:
					threads[message.thread_id] = self.s.mailbox.thread_metadata(message.thread_id)
				members = threads[message.thread_id]
				message.context_version = context_hash(members, config.sent_context_enabled)
				current_window = resolve_window(self.s.config, window.anchor_utc)
				newer_excluded = any(member.id != message.id and member.internal_ms > message.internal_ms and (not window.contains(member.internal_ms) or not current_window.contains(member.internal_ms)) and not ({"TRASH", "SPAM", "DRAFT"} & member.labels) for member in members)
				self.db.upsert_message(message)
				if stored.context_version != message.context_version or newer_excluded or message.missing_content or not message.body:
					self.db.execute("UPDATE predictions SET valid=0 WHERE message_id=?", (message.id,))
					self.db.execute("DELETE FROM manual_overrides WHERE message_id=?", (message.id,))
					self.db.execute("UPDATE messages SET defer_until_utc=NULL,deadline_utc=NULL WHERE id=?", (message.id,))
					self.save_recommendation(job, message, None, "review", "Conversation/content changed or is incomplete before AI submission", window)
					self.done(job, message.id, "classification")
					continue
			except Exception as exc:  # noqa: BLE001 - one classification preflight failure must preserve other targets
				self.fail(job, row, exc, config)
				continue
			direct = recommend(message, None, config, window, manual_override=self.s.override(message), rules=self.s.rules())
			if direct.action in {"keep", "skip", "archive"}:
				self.save_recommendation(job, message, None, direct.action, direct.reason, window, label_ids=direct.label_ids, approval="manual" if direct.evidence_source == "manual" else "automatic")
				self.done(job, message.id, "classification")
				continue
			examples = self.examples(message, config, window)
			route = self.route_category(message, config)
			model = config.optional_model_routes.get(route, config.ai_model)
			example_inputs = self.example_signature(examples)
			version = digest({"module": config.ai_module, "model": model, "examples": example_inputs, "prompt": PROMPT_VERSION, "sent_context_enabled": config.sent_context_enabled, "archived_context_enabled": config.archived_context_enabled})
			cached = self.db.one("SELECT assessment_json FROM predictions WHERE message_id=? AND content_hash=? AND context_version=? AND model_version=? AND valid=1 ORDER BY created_at DESC LIMIT 1", (message.id, message.content_hash, message.context_version, version))
			if cached:
				assessment = Assessment.from_dict(json.loads(cached["assessment_json"]))
				decision = recommend(message, assessment, config, window, manual_override=self.s.override(message), rules=self.s.rules())
				self.save_recommendation(job, message, assessment, decision.action, decision.reason, window, version, label_ids=decision.label_ids, approval="manual" if decision.evidence_source == "manual" else "automatic")
				self.done(job, message.id, "classification")
			else:
				# Batching unrelated example selections would make an assessment
				# depend on examples absent from its cache key. Group like selections.
				pending[(route, digest(example_inputs))].append((row, message, examples, version))
		if not pending:
			return
		exclusions = {"senders": config.excluded_senders, "domains": config.excluded_domains, "label_ids": config.excluded_label_ids, "categories": config.excluded_categories}
		context_options: dict[str, Any] = {"config": config}
		if getattr(self.s, "supports_context_cache", False):
			context_options.update(threads=threads, bodies={})
		for (route, _), group in pending.items():
			try:
				examples = group[0][2]
				contexts = {}
				for _, message, _, _ in group:
					for context in self.s.context_messages(message, window, **context_options):
						if window.contains(context.internal_ms) and resolve_window(self.s.config, window.anchor_utc).contains(context.internal_ms) and not self.excluded(context, config):
							contexts[context.id] = context
				if not self.reserve_ai_call(job, config):
					self.db.execute("UPDATE work_items SET state='pending' WHERE job_id=? AND stage='classification' AND state='running'", (job,))
					return
				batch = self.s.ai_for(route).assess([p[1] for p in group], self.db.labels(), examples=examples, exclusions=exclusions, sent_context_enabled=config.sent_context_enabled, context_messages=list(contexts.values()))
			except Exception as exc:  # noqa: BLE001 - provider group failure enters kind-aware job backoff without losing receipts
				for row, _, _, _ in group:
					self.fail(job, row, exc, config)
				continue
			for row, message, _, version in group:
				try:
					assessment = batch.assessments.get(message.id)
					if assessment is None:
						raise ValueError(batch.errors.get(message.id, "Missing assessment"))
					decision = recommend(message, assessment, config, window, manual_override=self.s.override(message), rules=self.s.rules())
					telemetry = {**batch.telemetry, "example_version": digest(self.example_signature(examples)), "route_category": route}
					with self.db.transaction():
						self.save_recommendation(job, message, assessment, decision.action, decision.reason, window, version, telemetry, label_ids=decision.label_ids, approval="manual" if decision.evidence_source == "manual" else "automatic")
						self.done(job, message.id, "classification")
				except Exception as exc:  # noqa: BLE001 - save failure retries only the unresolved classification item
					self.fail(job, row, exc, config)

	def save_recommendation(self, job, message, assessment, action, reason, window, version="", telemetry=None, label_ids=None, approval="automatic"):
		with self.db.transaction():
			if assessment:
				self.db.execute("UPDATE messages SET decision=?,category=?,deadline_utc=? WHERE id=?", (action, assessment.category, assessment.deadline_utc, message.id))
			else:
				self.db.execute("UPDATE messages SET decision=? WHERE id=?", (action, message.id))
			if assessment:
				insert(self.db, "predictions", {"id": identifier(), "message_id": message.id, "job_id": job, "content_hash": message.content_hash, "context_version": message.context_version, "policy_version": self.s.config.revision, "model_version": version, "prompt_version": PROMPT_VERSION, "example_version": (telemetry or {}).get("example_version", ""), "assessment_json": encode(assessment.to_dict()), "decision": action, "reason": reason, "valid": 1, "created_at": stamp(), "payload_json": encode(telemetry or {})})
			self.db.execute("UPDATE actions SET state='stale',reason='Superseded recommendation' WHERE message_id=? AND state IN ('queued','proposed') AND approval='automatic'", (message.id,))
			if action == "archive":
				labels = list(label_ids) if label_ids is not None else assessment.suggested_label_ids if assessment and self.s.config.auto_label else []
				bypass = (self.s.override(message) or {}).get("protection_bypass", []) if approval == "manual" else []
				self.s.actions.plan(job, message, "archive", labels, reason, approval, window, bypass)
			elif action in {"keep", "review"} and label_ids:
				bypass = (self.s.override(message) or {}).get("protection_bypass", []) if approval == "manual" else []
				self.s.actions.plan(job, message, "label", list(label_ids), reason, approval, window, bypass)

	def done(self, job: str, mid: str, stage: str) -> None:
		self.db.execute("UPDATE work_items SET state='completed',error='',next_attempt_utc=NULL WHERE job_id=? AND message_id=? AND stage=?", (job, mid, stage))

	def retry_deadline(self, job: str):
		"""Return an automatic retry deadline; user pauses never have one."""
		row = self.db.one("SELECT state,payload_json FROM jobs WHERE id=?", (job,))
		if not row:
			return None
		payload = json.loads(row["payload_json"])
		if row["state"] == "paused":
			pause = payload.get("pause", {})
			return datetime.fromisoformat(pause["due"]) if pause.get("origin") == "system" and pause.get("due") else None
		if row["state"] not in {"waiting", "retry"}:
			return None
		backoff = payload.get("backoff", {})
		if backoff.get("due"):
			return datetime.fromisoformat(backoff["due"])
		work = self.db.one("SELECT MIN(next_attempt_utc) due,COUNT(*) n,SUM(next_attempt_utc IS NULL) immediate FROM work_items WHERE job_id=? AND state='retry'", (job,))
		if not work or not work["n"]:
			return None
		return self.s.clock() if work["immediate"] else datetime.fromisoformat(work["due"]) if work["due"] else None

	def handle_exception(self, job: str, exc: Exception, config: Config) -> None:
		"""Keep operational pauses distinct from the user's explicit pause."""
		kind = getattr(exc, "kind", "") or ("rate_limit" if getattr(exc, "category", "") == "RateLimitError" else "network")
		if getattr(exc, "configuration", False):
			kind = "configuration"
		reason = safe_error(exc)
		if kind in {"auth", "permission", "account", "configuration", "data"}:
			self.pause_system(job, kind, reason)
			return
		if kind == "quota":
			now = self.s.clock()
			reset = float(getattr(exc, "retry_after", 0) or 0)
			quota_due = stamp(now + timedelta(seconds=reset)) if reset else stamp((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
			self.pause_system(job, kind, reason, due=quota_due)
			return
		payload = self.payload(job)
		attempt = int(payload.get("system_retries", 0)) + 1
		payload["system_retries"] = attempt
		self.save_payload(job, payload)
		delay = max(float(getattr(exc, "retry_after", 0) or 0), min(300, config.retry_base_seconds * 2 ** min(attempt, 8)) + random.random())  # noqa: S311 - retry jitter affects scheduling only, never security decisions
		due = stamp(self.s.clock() + timedelta(seconds=delay)) if attempt <= config.max_retries else None
		self.pause_system(job, kind, reason, due=due, attempts=attempt)

	def pause_system(self, job: str, kind: str, reason: str, *, due=None, attempts=0) -> None:
		payload = self.payload(job)
		payload["pause"] = {"origin": "system", "kind": kind, "reason": reason, "due": due, "attempts": attempts}
		self.save_payload(job, payload)
		self.db.execute("UPDATE jobs SET state='paused',updated_at=? WHERE id=?", (stamp(self.s.clock()), job))

	def fail(self, job: str, row: dict, exc: Exception, config: Config) -> None:
		current = self.db.one("SELECT state,attempts FROM work_items WHERE id=?", (row["id"],))
		if not current or current["state"] == "completed":
			return
		kind = getattr(exc, "kind", "") or ("rate_limit" if getattr(exc, "category", "") == "RateLimitError" else "")
		if getattr(exc, "configuration", False):
			kind = "configuration"
		if kind == "missing":
			self.db.execute("UPDATE messages SET in_inbox=0,decision='skip' WHERE id=?", (row["message_id"],))
			self.done(job, row["message_id"], row["stage"])
			return
		reason = safe_error(exc)
		attempt = current["attempts"]
		due = None
		state = "pending"
		if kind in {"auth", "permission", "account", "configuration", "quota"}:
			if kind == "quota":
				reset = float(getattr(exc, "retry_after", 0) or 0)
				now = self.s.clock()
				due = stamp(now + timedelta(seconds=reset)) if reset else stamp((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
			self.pause_system(job, kind, reason, due=due)
		elif kind == "rate_limit":
			payload = self.payload(job)
			backoff = payload.get("backoff", {})
			if not backoff.get("due") or datetime.fromisoformat(backoff["due"]) <= self.s.clock():
				round_number = int(backoff.get("attempts", 0)) + 1
				delay = max(float(getattr(exc, "retry_after", 0) or 0), min(300, config.retry_base_seconds * 2 ** min(round_number, 8)) + random.random())  # noqa: S311 - retry jitter affects scheduling only, never security decisions
				batch_size = config.ai_batch_size if row["stage"] == "classification" else config.retrieval_batch_size
				sizes = payload.setdefault("batch_sizes", {})
				sizes[row["stage"]] = max(1, sizes.get(row["stage"], batch_size) // 2)
				backoff = {"kind": kind, "due": stamp(self.s.clock() + timedelta(seconds=delay)), "attempts": round_number, "batch_size": sizes[row["stage"]]}
				payload["backoff"] = backoff
				self.save_payload(job, payload)
			due = backoff["due"]
			state = "retry"
			if backoff["attempts"] > config.max_retries:
				self.pause_system(job, kind, "Rate-limit retry budget exhausted; retry this job after checking quota")
			else:
				self.db.execute("UPDATE jobs SET state='waiting' WHERE id=?", (job,))
		else:
			attempt += 1
			unknown = getattr(exc, "category", "") in {"AIAPIsError", "UnknownProviderError", "InterruptedRequest"}
			retry = kind != "data" and not unknown and attempt <= config.max_retries
			delay = max(float(getattr(exc, "retry_after", 0) or 0), min(300, config.retry_base_seconds * 2 ** min(attempt, 8)) + random.random())  # noqa: S311 - retry jitter affects scheduling only, never security decisions
			due = stamp(self.s.clock() + timedelta(seconds=delay)) if retry else None
			state = "retry" if retry else "failed"
		self.db.execute("UPDATE work_items SET state=?,attempts=?,next_attempt_utc=?,error=? WHERE id=?", (state, attempt, due, reason, row["id"]))
		self.db.execute("UPDATE messages SET decision='review' WHERE id=? AND (decision IS NULL OR decision='pending')", (row["message_id"],))
		self.s.error(row["stage"], exc, job_id=job, message_id=row["message_id"])

	def summary(self, job: str) -> dict:
		result = {"id": job, "completed": 0, "failed": 0, "pending": 0, "retry": 0}
		for row in self.db.query("SELECT state,count(*) n FROM work_items WHERE job_id=? GROUP BY state", (job,)):
			result[row["state"]] = row["n"]
		state = self.db.one("SELECT state,stage FROM jobs WHERE id=?", (job,))
		result.update(state or {})
		payload = self.payload(job)
		if payload.get("ai_usage"):
			result["ai_usage"] = payload["ai_usage"]
		pause = payload.get("pause")
		if pause:
			result["pause"] = pause
		return result
