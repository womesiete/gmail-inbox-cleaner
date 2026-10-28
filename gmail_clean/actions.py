"""Message-scoped label ledger, preflight, uncertain-outcome recovery, and undo.

Gmail does not offer atomic compare-and-modify. Reads narrow the race; durable
attempts and readback make uncertainty visible without claiming exactly once.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from .domain import Window, context_hash, precondition_hash, protections
from .runtime import encode, identifier, insert, safe_error, stamp

if TYPE_CHECKING:
	from .service import AppService


class ActionExecutor:
	def __init__(self, service: AppService):
		self.s = service
		self.db = service.store
		self._history_evidence_cache = {}

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
		if self.s.mailbox.identity().casefold() != self.s.account_id.casefold():
			raise ValueError("Authenticated Gmail account does not match this action ledger")

	def live_state(self, action):
		message = self.s.mailbox.get_message(action["message_id"], body=False)
		members = self.s.mailbox.thread_metadata(message.thread_id)
		message.context_version = context_hash(members, self.s.config.sent_context_enabled)
		return message

	def preflight(self, action: dict):
		self.s.reload_config()
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

	def apply(self, action_ids=None, job_id=None) -> dict:
		self.s.reload_config()
		if self.s.config.dry_run:
			return {"applied": 0, "dry_run": True, "message": "Simulation: no mailbox changes"}
		self.verify_identity()
		self.s.refresh_labels()
		where = "state IN ('queued','proposed','uncertain','executing','retry')"
		params = []
		if action_ids is not None:
			if not action_ids:
				return {"applied": 0}
			where += f" AND id IN ({','.join('?' for _ in action_ids)})"
			params.extend(action_ids)
		if job_id:
			where += " AND job_id=?"
			params.append(job_id)
		actions = self.db.query("SELECT * FROM actions WHERE " + where + " ORDER BY created_at,id", params)
		groups = defaultdict(list)
		applied = 0
		for action in actions:
			try:
				if action["state"] in {"uncertain", "executing"}:
					self.recover(action)
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
				chunk = group[offset:offset + self.s.config.execution_batch_size]
				self.s.refresh_labels()
				ready = []
				# Recheck immediately before the request, rather than relying on a long
				# preflight for the first item of a 50,000-item execution queue.
				for action, _ in chunk:
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
				if self.s.config.dry_run:
					for action, _ in ready:
						self.mark(action["id"], "proposed", "Simulation enabled before the network write")
					continue
				unchanged = []
				for action, message in ready:
					if action["policy_version"] != self.s.config.revision:
						self.mark(action["id"], "conflict" if action["undo_of"] else "stale", "Settings changed during final preflight; replan")
					else:
						unchanged.append((action, message))
				ready = unchanged
				if not ready:
					continue
				# Persist the start of the write interval. A receipt checkpoint taken
				# only after readback could skip a concurrent user edit before undo.
				before_write_history_id = str(self.s.mailbox.profile()["historyId"])
				with self.db.transaction():
					for action, message in ready:
						attempt = identifier()
						insert(self.db, "attempts", {"id": attempt, "action_id": action["id"], "state": "sending", "before_json": encode(sorted(message.labels)), "started_at": stamp(), "payload_json": encode({"add": add, "remove": remove, "before_write_history_id": before_write_history_id})})
						self.db.execute("UPDATE actions SET state='executing',attempt_id=?,before_json=?,updated_at=? WHERE id=?", (attempt, encode(sorted(message.labels)), stamp(), action["id"]))
				try:
					self.s.mailbox.modify([action["message_id"] for action, _ in ready], list(add), list(remove))
				except Exception as exc:
					# Even a lost response can follow a successful Gmail commit.
					for action, _ in ready:
						self.mark(action["id"], "uncertain", "Write outcome unknown; reconcile before retry")
						self.s.error("execution", exc, job_id=action["job_id"], message_id=action["message_id"])
					continue
				for action, before in ready:
					try:
						after = self.s.mailbox.get_message(action["message_id"], body=False)
						if set(add) <= after.labels and not (set(remove) & after.labels):
							self.receipt(action, before, after, "applied", ownership="confirmed_response")
							applied += 1
						else:
							self.mark(action["id"], "conflict", "Readback did not confirm all requested effects")
					except Exception as exc:
						self.mark(action["id"], "uncertain", safe_error(exc))
		return {"applied": applied, "examined": len(actions), "dry_run": self.s.config.dry_run}

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
		if len(self._history_evidence_cache) >= 8:
			self._history_evidence_cache.pop(next(iter(self._history_evidence_cache)))
		self._history_evidence_cache[key] = result
		return result

	def receipt(self, action, before, after, state, ownership):
		payload = json.loads(action["payload_json"])
		end = str(self.s.mailbox.profile()["historyId"])
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

	def recover(self, action):
		self.verify_identity()
		message = self.s.mailbox.get_message(action["message_id"], body=False)
		add, remove = set(json.loads(action["add_json"])), set(json.loads(action["remove_json"]))
		if add <= message.labels and not (remove & message.labels):
			before = self.db.get_message(message.id)
			self.receipt(action, before, message, "reconciled", ownership="ambiguous_lost_response")
		elif not (add & message.labels) and remove <= message.labels:
			# The inverse state is observed. Replay is still subject to fresh preflight;
			# do not make another call in the same recovery operation.
			self.mark(action["id"], "retry", "Effects absent after uncertain attempt; explicit retry is available")
		else:
			self.mark(action["id"], "conflict", "Partial/ambiguous effects after uncertain attempt")

	def undo(self, action_ids) -> dict:
		queued = []
		for aid in action_ids:
			action = self.db.one("SELECT * FROM actions WHERE id=?", (aid,))
			if not action:
				continue
			if action["undo_of"]:
				continue
			if action["state"] in {"queued", "proposed", "retry", "stale"}:
				self.mark(aid, "cancelled", "Proposal cancelled by explicit undo")
				continue
			if action["state"] not in {"applied", "reconciled"}:
				continue
			# A settings change can stale an unexecuted undo preview. Cancel that
			# preview so a new explicit undo may use current restrictions.
			self.db.execute("UPDATE actions SET state='cancelled',reason='Stale undo preview superseded',updated_at=? WHERE undo_of=? AND state='stale'", (stamp(), aid))
			if self.db.one("SELECT id FROM actions WHERE undo_of=? AND state NOT IN ('cancelled','conflict')", (aid,)):
				continue
			payload = json.loads(action["payload_json"])
			if payload.get("ownership") != "confirmed_response":
				self.mark(aid, "conflict", "Lost response leaves app ownership ambiguous; inspect Gmail before undo")
				continue
			uid = identifier()
			payload["original_action"] = aid
			# Undo can restore only the recorded IDs/deltas, regardless of today's scope.
			insert(self.db, "actions", {"id": uid, "job_id": action["job_id"], "message_id": action["message_id"], "state": "proposed" if self.s.config.dry_run else "queued", "action": "undo", "add_json": action["remove_json"], "remove_json": action["add_json"], "before_json": action["after_json"], "context_version": action["context_version"], "policy_version": self.s.config.revision, "approval": "manual", "reason": "Explicit undo of recorded app-owned changes", "undo_of": aid, "created_at": stamp(), "updated_at": stamp(), "payload_json": encode(payload)})
			queued.append(uid)
		if queued and not self.s.config.dry_run:
			return self.apply(queued)
		return {"queued": len(queued), "dry_run": self.s.config.dry_run}

	def preflight_undo(self, action, message, payload, add, remove):
		original = self.db.one("SELECT * FROM actions WHERE id=?", (action["undo_of"],))
		if not original or original["state"] not in {"applied", "reconciled"}:
			self.mark(action["id"], "conflict", "Original change no longer owns an applicable undo")
			return None
		if remove & set(self.s.config.protected_label_ids):
			self.mark(action["id"], "conflict", "Undo would remove a currently protected label")
			return None
		if any(str(self.s.config.label_meanings.get(label, "")).strip().casefold() in {"protected", "requires_attention", "requires attention", "attention", "action"} for label in remove):
			self.mark(action["id"], "conflict", "Label now has an attention/protective meaning")
			return None
		# Ownership of a delta can be lost by an intervening edit even if the label
		# currently has the same value. History has no actor identity: ambiguity wins.
		checkpoint = payload.get("receipt_history_id")
		if not checkpoint:
			self.mark(action["id"], "conflict", "No history checkpoint is available to establish undo ownership")
			return None
		try:
			token = None
			while True:
				page = self.s.mailbox.history(str(checkpoint), token)
				for event in page.get("history", []):
					for kind in ("labelsAdded", "labelsRemoved", "messagesDeleted"):
						for change in event.get(kind, []):
							if change.get("message", {}).get("id") == message.id and ((set(change.get("labelIds", [])) & (add | remove)) or kind == "messagesDeleted"):
								self.mark(action["id"], "conflict", "Intervening relevant Gmail edits make undo ownership ambiguous")
								return None
				token = page.get("nextPageToken")
				if not token:
					break
		except Exception:
			self.mark(action["id"], "conflict", "Gmail history cannot establish safe undo ownership")
			return None
		expected = set(json.loads(original["after_json"]))
		if any((label in message.labels) != (label in expected) for label in add | remove):
			self.mark(action["id"], "conflict", "Recorded effects changed; inverse delta is unsafe")
			return None
		return message
