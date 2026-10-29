"""Short-lived job metadata snapshots, invalidated through Gmail history.

This cache is only for read stages. Execution preflights always use the live
mailbox directly. A failed/expired history interval never authorizes stale reuse.
"""
from __future__ import annotations

import time

from .domain import Message
from .gmail import HistoryExpired, PageTokenExpired


def _copy(message: Message) -> Message:
	return Message.from_dict(message.to_dict())


class StageMetadataCache:
	def __init__(self, mailbox, ttl_seconds=60, *, history_id=None, clock=time.monotonic):
		self.mailbox = mailbox
		self.ttl = ttl_seconds
		self.history_id = history_id
		self.clock = clock
		self.validated_at = float("-inf")
		self.messages: dict[str, tuple[float, Message]] = {}
		self.threads: dict[str, tuple[float, list[Message]]] = {}

	def refresh(self):
		"""Cover the entire history interval before using any cached evidence."""
		if not self.ttl:
			return
		if self.history_id is None:
			self.history_id = str(self.mailbox.profile()["historyId"])
			return
		token = None
		try:
			while True:
				page = self.mailbox.history(self.history_id, token)
				for event in page.get("history", []):
					changes = list(event.get("messages", []))
					for kind in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
						changes.extend(change.get("message", {}) for change in event.get(kind, []))
					for changed in changes:
						mid = str(changed.get("id", ""))
						old = self.messages.pop(mid, None)
						thread = changed.get("threadId") or (old[1].thread_id if old else None)
						if thread:
							self.threads.pop(str(thread), None)
						else:
							# A new/deleted member with no thread ID may belong to any
							# conversation we cached; conservatively refetch all threads.
							self.threads.clear()
				token = page.get("nextPageToken")
				if not token:
					self.history_id = str(page.get("historyId", self.history_id))
					# A fully covered, unchanged history interval renews the
					# evidence. Large backlogs need not redownload metadata just
					# because an earlier stage took longer than the TTL.
					self.validated_at = self.clock()
					break
		except (HistoryExpired, PageTokenExpired):
			self.messages.clear()
			self.threads.clear()
			self.history_id = str(self.mailbox.profile()["historyId"])

	def metadata(self, ids):
		self.refresh()
		now = self.clock()
		result = {}
		missing = []
		for mid in ids:
			cached = self.messages.get(mid)
			if self.ttl and cached and now - max(cached[0], self.validated_at) < self.ttl:
				result[mid] = _copy(cached[1])
			else:
				missing.append(mid)
		if missing:
			fetched = self.mailbox.get_messages(missing, body=False)
			for mid in missing:
				value = fetched.get(mid, ValueError("Missing metadata response"))
				result[mid] = value
				if isinstance(value, Message):
					self.messages[mid] = (now, _copy(value))
		return result

	def thread(self, thread_id):
		now = self.clock()
		cached = self.threads.get(thread_id)
		if self.ttl and cached and now - max(cached[0], self.validated_at) < self.ttl:
			return [_copy(message) for message in cached[1]]
		members = self.mailbox.thread_metadata(thread_id)
		self.threads[thread_id] = (now, [_copy(message) for message in members])
		return members
