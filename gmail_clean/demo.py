"""Reproducible offline mailbox; it has no Google client or credentials path.

All changes affect only this adapter's isolated local synthetic mailbox file.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
import weakref
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Iterable

from .domain import Message, as_utc, content_hash, epoch_ms, iso_utc
from .gmail import GmailError, HistoryExpired, PageTokenExpired

DEMO_ACCOUNT = "demo@example.invalid"
DEMO_LABELS = [
	{"id": name, "name": name, "type": "system"}
	for name in ("INBOX", "UNREAD", "STARRED", "IMPORTANT", "SENT", "DRAFT", "SPAM", "TRASH", "CATEGORY_PROMOTIONS")
] + [
	{"id": "label_news", "name": "Reference/Newsletters", "type": "user"},
	{"id": "label_records", "name": "Reference/Records", "type": "user"},
	{"id": "label_action", "name": "Needs attention", "type": "user"},
	{"id": "label_personal", "name": "Personal", "type": "user"},
]

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def _path_lock(path: Path | None):
	if path is None:
		return threading.RLock()
	key = str(path.resolve())
	with _LOCKS_GUARD:
		lock = _PATH_LOCKS.get(key)
		if lock is None:
			lock = threading.RLock()
			_PATH_LOCKS[key] = lock
		return lock


def _synchronized(method):
	"""Keep shared main/worker adapters consistent without reparsing unchanged data."""
	@wraps(method)
	def call(self, *args, **kwargs):
		with self._lock:
			if not self._read_depth:
				self._sync_from_disk()
			self._read_depth += 1
			try:
				return method(self, *args, **kwargs)
			finally:
				self._read_depth -= 1
	return call

# The same sender deliberately has completed records, outstanding requests, and
# uncertain follow-ups; a sender-only archive rule is never justified by this data.
_TEMPLATES = [
	("newsletter", "Research Bulletin <digest@science.example.invalid>", "Weekly research digest",
		"This week's research newsletter is ready for reference. No action is requested.\nList-only informational digest.", "research.digest.example.invalid"),
	("receipt", "Orders <receipts@store.example.invalid>", "Your paid receipt",
		"Payment received. Your order is complete and the receipt is provided for your records. No action required.", ""),
	("financial", "Case Desk <case@services.example.invalid>", "Insurance claim completed",
		"Your claim has been settled and payment processed. This completed financial record needs no further action.", ""),
	("work_request", "Case Desk <case@services.example.invalid>", "Please reply: confirm your availability",
		"Please reply with your availability before we can schedule the next step. Your response is still required.", ""),
	("promotion", "Shop Offers <offers@store.example.invalid>", "Offer expired",
		"This promotional discount ended last month. The offer has expired; no response is required.", "offers.store.example.invalid"),
	("medical", "Case Desk <case@services.example.invalid>", "Follow-up question: status unclear",
		"We sent a follow-up question. It is unclear whether the matter has been resolved. Please review the conversation.", ""),
	("personal", "Morgan <morgan@friends.example.invalid>", "Family plans: waiting for your answer",
		"Can you confirm whether you can join us? We are waiting for your answer before making the booking.", ""),
	("work_request", "Case Desk <case@services.example.invalid>", "Project completed — no action required",
		"The project is completed and the final documents have been accepted. No action required; keep this for reference.", ""),
	("event", "Community Events <events@community.example.invalid>", "Past event: community webinar",
		"This event ended last month. The webinar recording is available for reference. No action required.", "community.events.example.invalid"),
	("legal", "Records <records@counsel.example.invalid>", "Final signed agreement — completed",
		"All parties have signed. The agreement is final and completed. This important legal record needs no further action.", ""),
	("notification", "Parcel Status <status@delivery.example.invalid>", "Delivery notification",
		"Your parcel was delivered successfully. This is an automated notification. No action required.", ""),
	("work_request", "Team <team@work.example.invalid>", "Protected task: approval needed",
		"This task is still pending. Please approve the requested changes before the project can continue.", ""),
]


def generate_messages(count: int = 3000, anchor: datetime | None = None) -> Iterable[Message]:
	"""Generate exactly count synthetic messages, including mixed-age conversations."""
	anchor = as_utc(anchor or datetime.now(UTC))
	for index in range(max(0, int(count))):
		kind, sender, subject, text, list_id = _TEMPLATES[index % len(_TEMPLATES)]
		# Date distribution includes recent excluded items and old retained records.
		age_days = (index * 37) % 730
		instant = anchor - timedelta(days=age_days, seconds=(index * 137) % 86400)
		if index == 24:
			instant = datetime(1998, 12, 31, 12, tzinfo=UTC)
		message_id = f"d{index:015x}"
		thread_id = f"e{index:015x}"
		labels = {"INBOX"}
		if index % 3 == 0:
			labels.add("UNREAD")
		if index % 19 == 0:
			labels.add("IMPORTANT")
		if index % 41 == 0:
			labels.add("STARRED")
		if kind == "newsletter":
			labels.add("label_news")
		if kind in {"receipt", "financial", "legal"} and index % 2 == 0:
			labels.add("label_records")
		if index % 12 == 11:
			labels.add("label_action")
		if index % 12 == 4:
			labels.add("CATEGORY_PROMOTIONS")
		if index % 101 == 100:
			labels = {"SENT"}
		if index % 113 == 112:
			labels = {"DRAFT"}
		if index % 127 == 126:
			labels = {"SPAM"}
		if index % 139 == 138:
			labels = {"TRASH"}
		# Old request + excluded recent reply: root must inspect metadata and route
		# the old item to review without fetching/analyzing the new reply's body.
		if index % 200 == 30:
			thread_id = f"f{index // 200:015x}"
			instant = anchor - timedelta(days=120)
			subject = "Please reply: ongoing conversation"
			text = "An old request whose current resolution requires conversation context."
		if index % 200 == 31:
			thread_id = f"f{index // 200:015x}"
			instant = anchor - timedelta(days=2)
			subject = "Re: Please reply: ongoing conversation"
			text = "Recent excluded reply. Its private body must never reach classification in the default job."
		if index % 200 == 32:
			# Quiet message crosses the default two-week boundary the next day.
			instant = anchor - timedelta(days=13, hours=12)
		headers = {"from": sender, "to": DEMO_ACCOUNT, "subject": f"{subject} #{index // 12 + 1}",
			"message-id": f"<{message_id}@example.invalid>", "date": "Fri, 01 Jan 2099 00:00:00 +0000"}
		if list_id:
			headers["list-id"] = list_id
		message = Message(id=message_id, thread_id=thread_id, internal_ms=epoch_ms(instant),
			labels=labels, headers=headers, snippet=text[:120], body=text,
			missing_content=False, account_id=DEMO_ACCOUNT)
		message.content_hash = content_hash(message)
		yield message


class DemoMailbox:
	is_demo = True
	account_id = DEMO_ACCOUNT

	def __init__(self, state_path: str | Path | None = None, count: int = 3000,
			anchor: datetime | None = None) -> None:
		self.path = Path(state_path) if state_path is not None else None
		self._lock = _path_lock(self.path)
		self._read_depth = 0
		self._write_depth = 0
		self._stat_sig: tuple[int, int] | None = None
		self._history: list[dict] = []
		self._history_id = 1
		self.faults: dict[str, list[Exception]] = {}
		self.lose_next_modify_response = False
		self.body_reads: list[str] = []
		self.mutations: list[tuple[list[str], list[str], list[str]]] = []
		self._threads: dict[str, list[str]] = {}
		with self._disk_write_lock():
			if self.path is not None and self.path.exists():
				self._load_from_disk()
			else:
				self.anchor = as_utc(anchor or datetime.now(UTC))
				self.messages = {item.id: item for item in generate_messages(count, self.anchor)}
				self._labels = [dict(item) for item in DEMO_LABELS]
				self._rebuild_threads()
				self._save()

	def _rebuild_threads(self) -> None:
		self._threads = {}
		for item in self.messages.values():
			self._threads.setdefault(item.thread_id, []).append(item.id)

	def _load_from_disk(self) -> None:
		# fstat refers to the file actually read, even if another adapter replaces
		# the target concurrently. Its next signature check then sees that replace.
		with self.path.open("r", encoding="utf-8") as stream:
			payload = json.load(stream)
			stat = os.fstat(stream.fileno())
		if payload.get("account_id") != DEMO_ACCOUNT:
			raise GmailError("account", "This is not a synthetic demo mailbox file.")
		self.anchor = as_utc(payload["anchor"])
		self.messages = {item["id"]: Message.from_dict(item) for item in payload["messages"]}
		self._history = payload.get("history", [])
		self._history_id = int(payload.get("history_id", 1))
		self._labels = payload.get("labels", [dict(item) for item in DEMO_LABELS])
		self._rebuild_threads()
		self._stat_sig = (stat.st_mtime_ns, stat.st_size)

	def _sync_from_disk(self) -> None:
		if self.path is None:
			return
		try:
			stat = self.path.stat()
		except FileNotFoundError:
			return
		if (stat.st_mtime_ns, stat.st_size) != self._stat_sig:
			# Readers use the same cross-process protocol as writers while an
			# actual file handle is open. Windows can otherwise deny os.replace()
			# on an open reader even though the replacement itself is atomic.
			with self._disk_write_lock():
				try:
					stat = self.path.stat()
				except FileNotFoundError:
					return
				if (stat.st_mtime_ns, stat.st_size) != self._stat_sig:
					self._load_from_disk()

	@contextmanager
	def _disk_write_lock(self):
		"""Serialize read-modify-write across adapters and operating-system processes.

		The application also owns an instance lock, but direct demo adapters used
		by tests/tools must not overwrite one another's committed synthetic edits.
		"""
		with self._lock:
			if self.path is None or self._write_depth:
				self._write_depth += 1
				try:
					yield
				finally:
					self._write_depth -= 1
				return
			self.path.parent.mkdir(parents=True, exist_ok=True)
			lock_path = self.path.with_name(self.path.name + ".lock")
			with lock_path.open("a+b") as stream:
				if os.fstat(stream.fileno()).st_size == 0:
					stream.write(b"\0")
					stream.flush()
				stream.seek(0)
				if os.name == "nt":
					import msvcrt
					msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
				else:
					import fcntl
					fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
				self._write_depth += 1
				try:
					yield
				finally:
					self._write_depth -= 1
					stream.seek(0)
					if os.name == "nt":
						msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
					else:
						fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

	def _save(self) -> None:
		if self.path is None:
			return
		with self._disk_write_lock():
			payload = {"account_id": DEMO_ACCOUNT, "anchor": iso_utc(self.anchor),
				"history_id": self._history_id, "history": self._history,
				"labels": self._labels, "messages": [item.to_dict() for item in self.messages.values()]}
			temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
			try:
				with temporary.open("x", encoding="utf-8") as stream:
					json.dump(payload, stream, separators=(",", ":"), ensure_ascii=False)
					stream.flush()
					os.fsync(stream.fileno())
				os.replace(temporary, self.path)
				stat = self.path.stat()
				self._stat_sig = (stat.st_mtime_ns, stat.st_size)
			except Exception:
				# A failed commit must not leave uncommitted in-memory edits looking
				# current. The next public read reloads the last atomic disk version.
				self._stat_sig = None
				raise
			finally:
				temporary.unlink(missing_ok=True)

	@_synchronized
	def inject_error(self, operation: str, error: Exception) -> None:
		self.faults.setdefault(operation, []).append(error)

	def _fail(self, operation: str) -> None:
		if self.faults.get(operation):
			raise self.faults[operation].pop(0)

	@_synchronized
	def identity(self) -> str:
		self._fail("identity")
		return DEMO_ACCOUNT

	@_synchronized
	def profile(self) -> dict:
		self._fail("profile")
		return {"emailAddress": DEMO_ACCOUNT, "historyId": str(self._history_id),
			"messagesTotal": len(self.messages), "threadsTotal": len(self._threads)}

	@_synchronized
	def labels(self) -> list[dict]:
		self._fail("labels")
		return [dict(item) for item in self._labels]

	@_synchronized
	def list_messages(self, query: str, page_token: str | None = None, page_size: int = 500) -> dict:
		self._fail("list")
		offset = 0
		if page_token:
			try:
				prefix, generation, position = page_token.split(":")
				if prefix != "demo" or int(generation) != self._history_id:
					raise ValueError("stale page")
				offset = int(position)
			except (ValueError, TypeError):
				raise PageTokenExpired() from None
		after = re.search(r"\bafter:(-?\d+)", query)
		before = re.search(r"\bbefore:(-?\d+)", query)
		inbox = "in:inbox" in query.lower() or "label:inbox" in query.lower()
		items = [item for item in self.messages.values()
			if (not inbox or "INBOX" in item.labels)
			and not item.labels & {"SPAM", "TRASH", "DRAFT"}
			and (after is None or item.internal_ms > int(after[1]) * 1000)
			and (before is None or item.internal_ms < int(before[1]) * 1000)]
		items.sort(key=lambda item: (item.internal_ms, item.id), reverse=True)
		size = max(1, min(500, int(page_size)))
		page = items[offset:offset + size]
		result = {"messages": [{"id": item.id, "threadId": item.thread_id} for item in page],
			"resultSizeEstimate": len(items)}
		if offset + size < len(items):
			result["nextPageToken"] = f"demo:{self._history_id}:{offset + size}"
		return result

	@_synchronized
	def get_message(self, message_id: str, body: bool = False) -> Message:
		self._fail("get")
		if message_id not in self.messages:
			raise GmailError("missing", "The synthetic message no longer exists.")
		item = Message.from_dict(self.messages[message_id].to_dict())
		if body:
			self.body_reads.append(message_id)
		else:
			item.body = None
			item.content_hash = ""
			item.missing_content = True
		return item

	@_synchronized
	def get_messages(self, ids: list[str], body: bool = False) -> dict[str, Message | Exception]:
		self._fail("get_messages")
		result: dict[str, Message | Exception] = {}
		for message_id in dict.fromkeys(ids):
			try:
				result[message_id] = self.get_message(message_id, body)
			except Exception as exc:
				result[message_id] = exc
		return result

	@_synchronized
	def thread_metadata(self, thread_id: str) -> list[Message]:
		self._fail("thread")
		return [self.get_message(item, False) for item in self._threads.get(thread_id, []) if item in self.messages]

	@_synchronized
	def history(self, start: str, page_token: str | None = None) -> dict:
		self._fail("history")
		try:
			since = int(start)
			if since < 1 or since > self._history_id:
				raise ValueError("expired history")
		except (ValueError, TypeError):
			raise HistoryExpired() from None
		events = [item for item in self._history if int(item["id"]) > since]
		try:
			offset = int(page_token or 0)
		except (ValueError, TypeError):
			raise PageTokenExpired() from None
		result = {"history": events[offset:offset + 500], "historyId": str(self._history_id)}
		if offset + 500 < len(events):
			result["nextPageToken"] = str(offset + 500)
		return result

	@_synchronized
	def modify(self, ids: list[str], add: list[str], remove: list[str]) -> None:
		with self._disk_write_lock():
			self._sync_from_disk()
			self._fail("modify")
			allowed = {item["id"] for item in self._labels if item["type"] == "user"} | {"INBOX"}
			if not set(add).issubset(allowed) or not set(remove).issubset(allowed) or set(add) & set(remove):
				raise GmailError("permission", "Only INBOX and existing synthetic user labels may change.")
			if any(item not in self.messages for item in ids):
				raise GmailError("missing", "A synthetic message no longer exists.")
			for message_id in dict.fromkeys(ids):
				item = self.messages[message_id]
				added = set(add) - item.labels
				removed = set(remove) & item.labels
				if not added and not removed:
					continue
				item.labels = (item.labels | added) - removed
				self._history_id += 1
				event: dict = {"id": str(self._history_id)}
				message = {"id": item.id, "threadId": item.thread_id, "labelIds": sorted(item.labels)}
				if added:
					event["labelsAdded"] = [{"message": message, "labelIds": sorted(added)}]
				if removed:
					event["labelsRemoved"] = [{"message": message, "labelIds": sorted(removed)}]
				self._history.append(event)
			self.mutations.append((list(ids), sorted(set(add)), sorted(set(remove))))
			self._save()
			if self.lose_next_modify_response:
				self.lose_next_modify_response = False
				raise GmailError("network", "Synthetic lost response: the mailbox may already contain the change.")

	@_synchronized
	def add_message(self, item: Message) -> None:
		with self._disk_write_lock():
			self._sync_from_disk()
			if item.account_id and item.account_id != DEMO_ACCOUNT:
				raise GmailError("account", "Only synthetic messages belong in the demo.")
			item.account_id = DEMO_ACCOUNT
			self.messages[item.id] = item
			self._rebuild_threads()
			self._history_id += 1
			self._history.append({"id": str(self._history_id), "messagesAdded": [{"message": {
				"id": item.id, "threadId": item.thread_id, "labelIds": sorted(item.labels)}}]})
			self._save()
