"""Message-scoped label ledger, preflight, uncertain-outcome recovery, and undo.

Gmail does not offer atomic compare-and-modify. Reads narrow the race; durable
attempts and readback make uncertainty visible without claiming exactly once.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Callable

from .domain import Message, Window, context_hash, precondition_hash, protections
from .runtime import chunks, placeholders, digest, encode, identifier, insert, safe_error, stamp

if TYPE_CHECKING:
	from .service import AppService


class PlanConfirmationError(ValueError):
	"""A content-free explanation the desktop may display without redaction."""
	def __init__(self, message: str):
		super().__init__(message)
		self.safe_message = message


class ActionExecutor:
	def __init__(self, service: AppService):
		self.s = service
		self.db = service.store
		self._history_evidence_cache = {}
		self._prefetched_messages: dict[str, Message] = {}
		self._prefetched_threads: dict[str, list[Message]] = {}
		self._batch_identity_cache = False
		self._identity_verified = False
		self._should_stop: Callable[[], bool] | None = None
		self._undo_history_start = None
		self._undo_history_evidence = None
		self._reviewed_scope = None
		self._expected_dry_run = None

	def _stop_requested(self) -> bool:
		return bool(self._should_stop and self._should_stop())

	def _prefetch(self, message_ids: list[str]) -> None:
		"""Replace short-lived live-state caches at each safety/readback boundary.

		The Gmail adapter splits HTTP multipart retrieval into its configured
		batches. Transports lacking that optional bulk method retain the ordinary
		per-message path; an individual failed bulk part is also retried there.
		"""
		self._prefetched_messages.clear()
		self._prefetched_threads.clear()
		self._identity_verified = False
		self._undo_history_evidence = None
		if self._stop_requested():
			return
		if len(message_ids) > 1 and callable(getattr(self.s.mailbox, "get_messages", None)):
			try:
				results = self.s.mailbox.get_messages(message_ids, body=False)
			except Exception:
				# A failed multipart transport does not establish any item's state.
				# Individual preflight reads isolate and report affected failures.
				return
			self._prefetched_messages.update({mid: value for mid, value in results.items()
				if mid in message_ids and isinstance(value, Message) and value.id == mid})

	def plan(self, job_id, message, action, label_ids, reason, approval, window: Window, bypass=()) -> str:
		if action not in {"archive", "label"}:
			raise ValueError("Only archive and label actions may be planned")
		catalog = {label["id"] for label in self.db.labels() if label["type"] == "user"}
		if set(label_ids) - catalog:
			raise ValueError("Only existing user label IDs may be added")
		if not self.s.eligible(message, window):
			raise ValueError("Message is outside the permitted inbox examination scope")
		add = set(label_ids) - message.labels
		remove = {"INBOX"} if action == "archive" else set()
		if not add and not remove:
			return ""
		aid = identifier()
		payload = {"window": window.to_dict(), "protection_bypass": sorted(bypass), "scope": "explicit_message_ids", "history_id": self.db.checkpoint("history_id"), "ownership": "unconfirmed"}
		insert(self.db, "actions", {
			"id": aid, "job_id": job_id, "message_id": message.id, "state": "proposed" if self.s.config.dry_run else "queued",
			"action": action, "add_json": encode(sorted(add)), "remove_json": encode(sorted(remove)),
			"before_json": encode(sorted(message.labels)), "after_json": "[]", "context_version": message.context_version,
			"precondition_version": precondition_hash(message, self.s.config.revision), "policy_version": self.s.config.revision,
			"approval": approval, "reason": reason, "created_at": stamp(), "updated_at": stamp(), "payload_json": encode(payload),
		})
		return aid

	def mark(self, aid: str, state: str, reason: str | None = None) -> None:
		if reason is None:
			self.db.execute("UPDATE actions SET state=?,updated_at=? WHERE id=?", (state, stamp(), aid))
		else:
			self.db.execute("UPDATE actions SET state=?,reason=?,updated_at=? WHERE id=?", (state, reason, stamp(), aid))

	def verify_identity(self) -> None:
		if self._batch_identity_cache and self._identity_verified:
			return
		if self.s.mailbox.identity().casefold() != self.s.account_id.casefold():
			raise ValueError("Authenticated Gmail account does not match this action ledger")
		if self._batch_identity_cache:
			self._identity_verified = True

	def _checkpoint(self) -> str:
		profile = self.s.mailbox.profile()
		identity = profile.get("emailAddress")
		if identity is not None and str(identity).casefold() != self.s.account_id.casefold():
			raise ValueError("Authenticated Gmail account does not match this action ledger")
		return str(profile["historyId"])

	def live_state(self, action):
		message = self._prefetched_messages.pop(action["message_id"], None)
		if message is None:
			message = self.s.mailbox.get_message(action["message_id"], body=False)
		if self._batch_identity_cache:
			if message.thread_id not in self._prefetched_threads:
				self._prefetched_threads[message.thread_id] = self.s.mailbox.thread_metadata(message.thread_id)
			members = self._prefetched_threads[message.thread_id]
		else:
			members = self.s.mailbox.thread_metadata(message.thread_id)
		message.context_version = context_hash(members, self.s.config.sent_context_enabled)
		return message

	def preflight(self, action: dict):
		self.s.reload_config()
		# Rule/example edits and explicit review can invalidate the durable action
		# without changing Config.revision. Never trust the earlier queue snapshot.
		if not self._pending(action):
			return None
		if self.s.config.dry_run:
			self.mark(action["id"], "proposed", "Simulation enabled before execution; no mailbox write")
			return None
		self.verify_identity()
		if action["account_id"] != self.s.account_id:
			self.mark(action["id"], "conflict", "Action account mismatch")
			return None
		message = self.live_state(action)
		payload = json.loads(action["payload_json"])
		add, remove = set(json.loads(action["add_json"])), set(json.loads(action["remove_json"]))
		catalog = {item["id"] for item in self.db.labels()}
		if (add | (remove - {"INBOX"})) - catalog:
			self.mark(action["id"], "conflict", "A required label was deleted; refresh/replan")
			return None
		if action["undo_of"]:
			return self.preflight_undo(action, message, payload, add, remove)
		if add <= message.labels and not (remove & message.labels):
			# Existing effects can come from another Gmail client. Reconcile them
			# without claiming ownership or making an unnecessary network write.
			self.mark(action["id"], "satisfied", "Effects already present; no new ownership claimed")
			return None
		window = Window.from_dict(payload["window"])
		if not self.s.eligible(message, window):
			self.mark(action["id"], "stale", "No longer an eligible inbox target under frozen and current restrictions")
			return None
		if action["policy_version"] != self.s.config.revision:
			self.mark(action["id"], "stale", "Settings changed; replan with the current policy")
			return None
		if message.context_version != action["context_version"]:
			self.mark(action["id"], "stale", "Conversation changed since assessment/approval")
			return None
		bypass = set(payload.get("protection_bypass", [])) if action["approval"] == "manual" else set()
		if protections(message, self.s.config) - bypass:
			self.mark(action["id"], "stale", "A current protection was not deliberately overridden")
			return None
		override = self.s.override(message)
		if override and override.get("action") in {"keep", "defer"}:
			self.mark(action["id"], "stale", "A current explicit keep/deferral takes precedence")
			return None
		# Unrelated labels/read state may change safely; action-specific labels may not.
		before = set(json.loads(action["before_json"]))
		if any((label in message.labels) != (label in before) for label in add | remove):
			self.mark(action["id"], "conflict", "Action-specific label state changed before execution")
			return None
		return message

	def _pending(self, action: dict) -> bool:
		current = self.db.one("SELECT state FROM actions WHERE id=?", (action["id"],))
		return bool(current and current["state"] in {"queued", "proposed", "retry"})

	@staticmethod
	def _intent_hash(action) -> str:
		keys = ("id", "account_id", "job_id", "message_id", "action", "add_json", "remove_json", "before_json", "context_version", "precondition_version", "policy_version", "approval", "undo_of", "payload_json")
		return digest({key: action[key] for key in keys})

	def _assert_execution_mode(self) -> None:
		if self._expected_dry_run is not None and self.s.config.dry_run != self._expected_dry_run:
			raise PlanConfirmationError("Execution mode changed; review and confirm the current simulation/live setting before executing this plan")

	def _assert_reviewed_scope(self, expected_hash=None) -> None:
		if self._reviewed_scope is None:
			return
		action_ids, job_id, approved_ids = self._reviewed_scope
		snapshot = self.plan_snapshot(action_ids, job_id)
		if (expected_hash is not None and snapshot["hash"] != expected_hash) or not set(snapshot["action_ids"]) <= approved_ids:
			raise PlanConfirmationError("The reviewed plan changed during preflight; review its count and hash again")

	def _assert_intents_unchanged(self, ready) -> None:
		self._assert_reviewed_scope()
		for action, _ in ready:
			current = self.db.one("SELECT * FROM actions WHERE id=?", (action["id"],))
			if current is None or self._intent_hash(current) != self._intent_hash(action):
				raise PlanConfirmationError("The reviewed plan changed during preflight; review its count and hash again")

	def plan_snapshot(self, action_ids=None, job_id=None) -> dict:
		"""Hash the exact reviewed target IDs and write intents, independent of simulation."""
		where = "state IN ('queued','proposed','uncertain','executing','retry')"
		params = []
		if job_id:
			where += " AND job_id=?"
			params.append(job_id)
		if action_ids is None:
			rows = self.db.query("SELECT * FROM actions WHERE " + where + " ORDER BY id", params)  # noqa: S608 -- internal clauses only; values stay bound
		else:
			rows = []
			for selected in chunks(dict.fromkeys(action_ids)):
				rows.extend(self.db.query("SELECT * FROM actions WHERE " + where + f" AND id IN ({placeholders(selected)})", [*params, *selected]))  # noqa: S608 -- generated placeholders only; values stay bound
			rows.sort(key=lambda row: row["id"])
		keys = ("id", "account_id", "job_id", "message_id", "action", "add_json", "remove_json", "before_json", "context_version", "precondition_version", "policy_version", "approval", "undo_of", "payload_json")
		return {"count": len(rows), "hash": digest([{key: row[key] for key in keys} for row in rows]), "action_ids": [row["id"] for row in rows]}

	def apply(self, action_ids=None, job_id=None, should_stop: Callable[[], bool] | None = None, *, expected_plan_hash: str | None = None, allow_over_limit: bool = False, expected_dry_run: bool | None = None) -> dict:
		self._batch_identity_cache = True
		self._identity_verified = False
		self._should_stop = should_stop
		self._expected_dry_run = expected_dry_run
		try:
			self._assert_execution_mode()
			action_ids = list(action_ids) if action_ids is not None else None
			snapshot = self.plan_snapshot(action_ids, job_id)
			if expected_plan_hash is not None and snapshot["hash"] != expected_plan_hash:
				raise PlanConfirmationError("The reviewed plan changed; inspect the count and hash again before execution")
			if not self.s.config.dry_run and snapshot["count"] > self.s.config.max_actions_per_run and not allow_over_limit:
				raise PlanConfirmationError(f"Plan of {snapshot['count']} actions exceeds the write cap of {self.s.config.max_actions_per_run}; explicit confirmation is required")
			self._reviewed_scope = (action_ids, job_id, set(snapshot["action_ids"])) if expected_plan_hash is not None else None
			# Freeze selection for every invocation, including unattended execution:
			# a network callback must never add targets beyond the checked cap.
			return self._apply(snapshot["action_ids"], job_id, reviewed_hash=expected_plan_hash)
		finally:
			# Neither another command nor a canceled/failed request may reuse stale
			# live state, identity evidence, or a prior command's stop callback.
			self._prefetched_messages.clear()
			self._prefetched_threads.clear()
			self._batch_identity_cache = False
			self._identity_verified = False
			self._should_stop = None
			self._reviewed_scope = None
			self._expected_dry_run = None

	def _apply(self, action_ids=None, job_id=None, *, reviewed_hash=None) -> dict:
		self.s.reload_config()
		self._assert_execution_mode()
		if self.s.config.dry_run:
			return {"applied": 0, "dry_run": True, "message": "Simulation: no mailbox changes"}
		if self._stop_requested():
			return {"applied": 0, "dry_run": False, "stopped": True}
		self.verify_identity()
		self.s.refresh_labels()
		self._assert_reviewed_scope(reviewed_hash)
		where = "state IN ('queued','proposed','uncertain','executing','retry')"
		params = []
		if job_id:
			where += " AND job_id=?"
			params.append(job_id)
		if action_ids is None:
			actions = self.db.query("SELECT * FROM actions WHERE " + where + " ORDER BY created_at,id", params)  # noqa: S608 -- internal state clauses and bind markers only; values stay bound
		else:
			actions = []
			for selected in chunks(dict.fromkeys(action_ids)):
				actions.extend(self.db.query("SELECT * FROM actions WHERE " + where + f" AND id IN ({placeholders(selected)})", [*params, *selected]))  # noqa: S608 -- internal state clauses and bind markers only; values stay bound
			actions.sort(key=lambda row: (row["created_at"], row["id"]))

		starts = []
		for action in actions:
			if action["undo_of"]:
				checkpoint = json.loads(action["payload_json"]).get("receipt_history_id")
				if str(checkpoint).isdigit():
					starts.append(int(checkpoint))
		self._undo_history_start = min(starts) if starts else None
		groups = defaultdict(list)
		applied = 0
		self._prefetch([action["message_id"] for action in actions])
		recovery_checkpoint = self._checkpoint() if any(action["state"] in {"uncertain", "executing"} for action in actions) else None
		for action in actions:
			if self._stop_requested():
				break
			try:
				if action["state"] in {"uncertain", "executing"}:
					self.recover(action, end_history_id=recovery_checkpoint)
					continue
				message = self.preflight(action)
				if message is None:
					continue
				add = tuple(sorted(set(json.loads(action["add_json"])) - message.labels))
				remove = tuple(sorted(set(json.loads(action["remove_json"])) & message.labels))
				if not add and not remove:
					self.mark(action["id"], "satisfied", "Effects already present; no new ownership claimed")
					continue
				groups[(add, remove)].append((action, message))
			except Exception as exc:
				self.s.error("preflight", exc, job_id=action["job_id"], message_id=action["message_id"])
				self.mark(action["id"], "conflict", safe_error(exc))
		for (add, remove), group in groups.items():
			for offset in range(0, len(group), self.s.config.execution_batch_size):
				if self._stop_requested():
					return {"applied": applied, "examined": len(actions), "dry_run": self.s.config.dry_run, "stopped": True}
				chunk = group[offset:offset + self.s.config.execution_batch_size]
				self._prefetch([action["message_id"] for action, _ in chunk])
				self.s.refresh_labels()
				ready = []
				# Recheck immediately before the request, rather than relying on a long
				# preflight for the first item of a 50,000-item execution queue.
				for action, _ in chunk:
					if self._stop_requested():
						break
					try:
						message = self.preflight(action)
						if message is not None:
							ready.append((action, message))
					except Exception as exc:
						self.mark(action["id"], "conflict", safe_error(exc))
				if not ready:
					continue
				# Configuration may change while another item in this chunk is
				# checked. Apply the latest restrictions to every ready item.
				self.s.reload_config()
				self._assert_execution_mode()
				if self.s.config.dry_run:
					for action, _ in ready:
						self.mark(action["id"], "proposed", "Simulation enabled before the network write")
					continue
				unchanged = []
				for action, message in ready:
					if not self._pending(action):
						continue
					if action["policy_version"] != self.s.config.revision:
						self.mark(action["id"], "conflict" if action["undo_of"] else "stale", "Settings changed during final preflight; replan")
					else:
						unchanged.append((action, message))
				ready = unchanged
				if not ready:
					continue
				if self._stop_requested():
					return {"applied": applied, "examined": len(actions), "dry_run": self.s.config.dry_run, "stopped": True}
				# Persist the start of the write interval. A receipt checkpoint taken
				# only after readback could skip a concurrent user edit before undo.
				self._assert_intents_unchanged(ready)
				before_write_history_id = self._checkpoint()
				with self.db.transaction():
					self.s.reload_config()
					self._assert_execution_mode()
					if self.s.config.dry_run:
						for action, _ in ready:
							self.mark(action["id"], "proposed", "Simulation enabled at the last write checkpoint")
						ready = []
					self._assert_intents_unchanged(ready)
					# Check again after the network checkpoint, under the transaction
					# that claims these intents. Another local worker may have edited
					# a matching rule while the profile request was in flight.
					ready = [(action, message) for action, message in ready if self._pending(action)]
					for action, message in ready:
						attempt = identifier()
						insert(self.db, "attempts", {"id": attempt, "action_id": action["id"], "state": "sending", "before_json": encode(sorted(message.labels)), "started_at": stamp(), "payload_json": encode({"add": add, "remove": remove, "before_write_history_id": before_write_history_id})})
						self.db.execute("UPDATE actions SET state='executing',attempt_id=?,before_json=?,updated_at=? WHERE id=?", (attempt, encode(sorted(message.labels)), stamp(), action["id"]))
				if not ready:
					continue
				# A durable attempt is not permission to ignore a newly enabled
				# simulation setting while the request was still unsent.
				self.s.reload_config()
				if self.s.config.dry_run:
					with self.db.transaction():
						for action, _ in ready:
							self.db.execute("UPDATE attempts SET state='cancelled_before_send',finished_at=? WHERE action_id=? AND state='sending'", (stamp(), action["id"]))
							self.mark(action["id"], "proposed", "Simulation enabled before the network write")
					self._assert_execution_mode()
					continue
				self._assert_execution_mode()
				try:
					if self._stop_requested():
						with self.db.transaction():
							for action, _ in ready:
								self.db.execute("UPDATE attempts SET state='cancelled_before_send',finished_at=? WHERE action_id=? AND state='sending'", (stamp(), action["id"]))
								self.mark(action["id"], "queued", "Stopped before the network write")
						return {"applied": applied, "examined": len(actions), "dry_run": self.s.config.dry_run, "stopped": True}
					self.s.mailbox.modify([action["message_id"] for action, _ in ready], list(add), list(remove))
				except Exception as exc:
					status = getattr(exc, "status", None)
					if status is not None and 400 <= status < 500 and status != 408:
						# An explicit rejection has no successful response to own. Cheap
						# bulk readback distinguishes existing effects before any replay.
						self._prefetch([action["message_id"] for action, _ in ready])
						if status in {400, 404} and callable(getattr(self.s, "invalidate_labels", None)):
							self.s.invalidate_labels()
							self.s.refresh_labels()
						retry_ids = []
						with self.db.transaction():
							for action, _ in ready:
								self.db.execute("UPDATE attempts SET state='not_applied',error=?,finished_at=? WHERE action_id=? AND state='sending'", (safe_error(exc), stamp(), action["id"]))
								observed = self._prefetched_messages.get(action["message_id"])
								if observed is not None and set(add) <= observed.labels and not (set(remove) & observed.labels):
									self.mark(action["id"], "satisfied", "Effects present after rejected request; no ownership claimed")
								elif status in {400, 404, 422} and len(ready) == 1:
									self.mark(action["id"], "conflict", "Gmail definitely rejected this target; inspect deleted IDs or invalid labels")
								else:
									self.mark(action["id"], "retry", "Gmail rejected the write; no uncertain attempt remains")
									retry_ids.append(action["id"])
						if status in {400, 404, 422} and len(ready) > 1 and retry_ids:
							middle = max(1, len(retry_ids) // 2)
							for subset in (retry_ids[:middle], retry_ids[middle:]):
								if subset and not self._stop_requested():
									applied += self._apply(subset)["applied"]
						continue
					# A lost response can follow a successful Gmail commit; only these
					# transport/server outcomes require uncertain-write reconciliation.
					for action, _ in ready:
						self.mark(action["id"], "uncertain", "Write outcome unknown; reconcile before retry")
						self.s.error("execution", exc, job_id=action["job_id"], message_id=action["message_id"])
					continue
				if self._stop_requested():
					for action, _ in ready:
						self.mark(action["id"], "uncertain", "Stopped after write; reconcile before retry")
					return {"applied": applied, "examined": len(actions), "dry_run": self.s.config.dry_run, "stopped": True}
				try:
					after_write_history_id = self._checkpoint()
				except Exception as exc:
					for action, _ in ready:
						self.mark(action["id"], "uncertain", safe_error(exc))
					continue
				self._prefetch([action["message_id"] for action, _ in ready])
				for index, (action, before) in enumerate(ready):
					if self._stop_requested():
						for pending, _ in ready[index:]:
							self.mark(pending["id"], "uncertain", "Stopped after write; reconcile before retry")
						return {"applied": applied, "examined": len(actions), "dry_run": self.s.config.dry_run, "stopped": True}
					try:
						after = self._prefetched_messages.pop(action["message_id"], None)
						if after is None:
							after = self.s.mailbox.get_message(action["message_id"], body=False)
						if set(add) <= after.labels and not (set(remove) & after.labels):
							self.receipt(action, before, after, "applied", ownership="confirmed_response", end_history_id=after_write_history_id)
							applied += 1
						else:
							self.mark(action["id"], "conflict", "Readback did not confirm all requested effects")
					except Exception as exc:
						self.mark(action["id"], "uncertain", safe_error(exc))
		return {"applied": applied, "examined": len(actions), "dry_run": self.s.config.dry_run, "stopped": self._stop_requested()}

	def _history_interval(self, start, end):
		"""Read exact specific transitions through a captured checkpoint, with replay deduplication."""
		key = (str(start), str(end))
		if key in self._history_evidence_cache:
			return self._history_evidence_cache[key]
		try:
			start_number, end_number = int(start), int(end)
			if start_number > end_number:
				raise ValueError("Invalid history interval")
			token = None
			tokens = set()
			events = []
			seen = set()
			for _ in range(100):
				if self._stop_requested():
					raise ValueError("Receipt inspection interrupted")
				page = self.s.mailbox.history(str(start), token)
				if int(page.get("historyId", "0")) < end_number:
					raise ValueError("History did not cover the receipt checkpoint")
				for event in page.get("history", []):
					history_number = int(event["id"])
					if not start_number < history_number <= end_number:
						continue
					for kind in ("labelsAdded", "labelsRemoved", "messagesAdded", "messagesDeleted"):
						for change in event.get(kind, []):
							mid = change.get("message", {}).get("id", "")
							for label in change.get("labelIds", []) if kind.startswith("labels") else [""]:
								transition = (history_number, kind, mid, label)
								if transition not in seen:
									seen.add(transition)
									events.append(transition)
				token = page.get("nextPageToken")
				if not token:
					result = (True, events)
					break
				if token in tokens:
					raise ValueError("Repeated Gmail history page token")
				tokens.add(token)
			else:
				raise ValueError("History interval exceeds the bounded receipt inspection")
		except Exception:
			result = (False, [])
		if self._stop_requested():
			return result
		if len(self._history_evidence_cache) >= 8:
			self._history_evidence_cache.pop(next(iter(self._history_evidence_cache)))
		self._history_evidence_cache[key] = result
		return result

	def receipt(self, action, before, after, state, ownership, *, end_history_id: str | None = None):
		payload = json.loads(action["payload_json"])
		end = str(end_history_id) if end_history_id is not None else self._checkpoint()
		payload["receipt_history_id"] = end
		attempt = self.db.one("SELECT t.payload_json FROM attempts t JOIN actions a ON a.attempt_id=t.id WHERE a.id=?", (action["id"],))
		attempt_payload = json.loads(attempt["payload_json"]) if attempt else {}
		start = attempt_payload.get("before_write_history_id")
		evidence = {"start_history_id": start, "end_history_id": end, "reason": "no_durable_write_interval"}
		if start is not None:
			reliable, events = self._history_interval(start, end)
			add, remove = set(json.loads(action["add_json"])), set(json.loads(action["remove_json"]))
			relevant = [(kind, label) for _, kind, mid, label in events if mid == action["message_id"] and (label in add | remove or kind.startswith("messages"))]
			counts = Counter(relevant)
			expected = Counter({("labelsAdded", label): 1 for label in add}) + Counter({("labelsRemoved", label): 1 for label in remove})
			exact = reliable and counts == expected
			evidence["reason"] = "exact_expected_transitions" if exact else "missing_or_conflicting_transitions" if reliable else "history_unavailable"
			evidence["observed"] = [{"kind": kind, "label_id": label, "count": count} for (kind, label), count in sorted(counts.items())]
		else:
			exact = False
		if ownership == "confirmed_response" and not exact:
			ownership = "ambiguous_history"
			payload["ownership_warning"] = "Effects are present, but history cannot establish exclusive app ownership for undo."
		payload["ownership"] = ownership
		payload["ownership_evidence"] = evidence
		with self.db.transaction():
			self.db.execute("UPDATE actions SET state=?,after_json=?,payload_json=?,updated_at=? WHERE id=?", (state, encode(sorted(after.labels)), encode(payload), stamp(), action["id"]))
			self.db.execute("UPDATE attempts SET state=?,after_json=?,finished_at=? WHERE action_id=? AND state='sending'", (state, encode(sorted(after.labels)), stamp(), action["id"]))
			old = self.db.get_message(after.id)
			if old:
				after.body, after.context_version = old.body, old.context_version
				after.content_hash = old.content_hash
			self.db.upsert_message(after)
			if action["undo_of"]:
				self.mark(action["undo_of"], "undone")

	def recover(self, action, *, end_history_id=None):
		self.verify_identity()
		message = self._prefetched_messages.pop(action["message_id"], None)
		if message is None:
			message = self.s.mailbox.get_message(action["message_id"], body=False)
		add, remove = set(json.loads(action["add_json"])), set(json.loads(action["remove_json"]))
		if add <= message.labels and not (remove & message.labels):
			before = self.db.get_message(message.id)
			self.receipt(action, before, message, "reconciled", ownership="ambiguous_lost_response", end_history_id=end_history_id)
		elif not (add & message.labels) and remove <= message.labels:
			# The inverse state is observed. Replay is still subject to fresh preflight;
			# do not make another call in the same recovery operation.
			self.mark(action["id"], "retry", "Effects absent after uncertain attempt; explicit retry is available")
		else:
			self.mark(action["id"], "conflict", "Partial/ambiguous effects after uncertain attempt")

	def undo(self, action_ids, should_stop: Callable[[], bool] | None = None, *, allow_unverified=False, confirmed=False, allow_over_limit=False) -> dict:
		if allow_unverified and not confirmed:
			raise PlanConfirmationError("Ownership-unverified undo requires explicit confirmation")
		queued = []
		for aid in action_ids:
			if should_stop and should_stop():
				break
			action = self.db.one("SELECT * FROM actions WHERE id=?", (aid,))
			if not action:
				continue
			if action["undo_of"]:
				continue
			if action["state"] in {"queued", "proposed", "retry", "stale"}:
				self.mark(aid, "cancelled", "Proposal cancelled by explicit undo")
				continue
			payload = json.loads(action["payload_json"])
			unverified_receipt = allow_unverified and action["state"] == "conflict" and payload.get("receipt_history_id") is not None
			if action["state"] not in {"applied", "reconciled"} and not unverified_receipt:
				continue
			# A settings change can stale an unexecuted undo preview. Cancel that
			# preview so a new explicit undo may use current restrictions.
			self.db.execute("UPDATE actions SET state='cancelled',reason='Stale undo preview superseded',updated_at=? WHERE undo_of=? AND state='stale'", (stamp(), aid))
			existing = self.db.one("SELECT id,state,payload_json FROM actions WHERE undo_of=? AND state NOT IN ('cancelled','conflict')", (aid,))
			if existing:
				if existing["state"] in {"uncertain", "executing"}:
					queued.append(existing["id"])
					continue
				if existing["state"] in {"queued", "proposed", "retry"}:
					same_authorization = bool(json.loads(existing["payload_json"]).get("ownership_unverified_confirmation")) == bool(allow_unverified)
					if same_authorization:
						queued.append(existing["id"])
						continue
					self.mark(existing["id"], "cancelled", "Explicit undo authorization mode changed")
				else:
					continue
			payload = json.loads(action["payload_json"])
			if payload.get("ownership") != "confirmed_response" and not allow_unverified:
				self.mark(aid, "conflict", "Lost response leaves app ownership ambiguous; inspect Gmail before undo")
				continue
			uid = identifier()
			payload["original_action"] = aid
			payload["ownership_unverified_confirmation"] = bool(allow_unverified and confirmed)
			# Undo can restore only the recorded IDs/deltas, regardless of today's scope.
			insert(self.db, "actions", {"id": uid, "job_id": action["job_id"], "message_id": action["message_id"], "state": "proposed" if self.s.config.dry_run else "queued", "action": "undo", "add_json": action["remove_json"], "remove_json": action["add_json"], "before_json": action["after_json"], "context_version": action["context_version"], "policy_version": self.s.config.revision, "approval": "manual", "reason": "Explicit undo of recorded app-owned changes", "undo_of": aid, "created_at": stamp(), "updated_at": stamp(), "payload_json": encode(payload)})
			queued.append(uid)
		if queued and not self.s.config.dry_run:
			return self.apply(queued, should_stop=should_stop, allow_over_limit=allow_over_limit)
		return {"queued": len(queued), "dry_run": self.s.config.dry_run, "stopped": bool(should_stop and should_stop())}

	def preflight_undo(self, action, message, payload, add, remove):
		original = self.db.one("SELECT * FROM actions WHERE id=?", (action["undo_of"],))
		if not original or (original["state"] not in {"applied", "reconciled"} and not (original["state"] == "conflict" and payload.get("ownership_unverified_confirmation"))):
			self.mark(action["id"], "conflict", "Original change no longer owns an applicable undo")
			return None
		if remove & set(self.s.config.protected_label_ids):
			self.mark(action["id"], "conflict", "Undo would remove a currently protected label")
			return None
		if any(str(self.s.config.label_meanings.get(label, "")).strip().casefold() in {"protected", "requires_attention", "requires attention", "attention", "action"} for label in remove):
			self.mark(action["id"], "conflict", "Label now has an attention/protective meaning")
			return None
		if payload.get("ownership_unverified_confirmation"):
			if message.labels != set(json.loads(original["after_json"])):
				self.mark(action["id"], "conflict", "Ownership-unverified undo requires the exact recorded after-state")
				return None
			return message
		# Ownership of a delta can be lost by an intervening edit even if the label
		# currently has the same value. History has no actor identity: ambiguity wins.
		checkpoint = payload.get("receipt_history_id")
		if not checkpoint or not str(checkpoint).isdigit():
			self.mark(action["id"], "conflict", "No history checkpoint is available to establish undo ownership")
			return None
		if self._undo_history_evidence is None:
			start = self._undo_history_start if self._undo_history_start is not None else int(checkpoint)
			reliable, events = self._history_interval(start, self._checkpoint())
			index = defaultdict(list)
			for number, kind, mid, label in events:
				index[mid].append((number, kind, label))
			self._undo_history_evidence = (reliable, index)
		reliable, index = self._undo_history_evidence
		if self._stop_requested():
			self.mark(action["id"], "queued", "Undo paused before history inspection finished")
			return None
		if not reliable:
			self.mark(action["id"], "conflict", "Gmail history cannot establish safe undo ownership; finite retention may require explicitly confirmed ownership-unverified undo")
			return None
		for number, kind, label in index.get(message.id, []):
			if number > int(checkpoint) and (label in add | remove or kind.startswith("messages")):
				self.mark(action["id"], "conflict", "Intervening relevant Gmail edits make undo ownership ambiguous")
				return None
		expected = set(json.loads(original["after_json"]))
		if any((label in message.labels) != (label in expected) for label in add | remove):
			self.mark(action["id"], "conflict", "Recorded effects changed; inverse delta is unsafe")
			return None
		return message
