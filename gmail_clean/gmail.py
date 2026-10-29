"""Message-scoped Gmail transport. No thread-wide or destructive write methods."""
from __future__ import annotations

import json
import random
import ssl
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Protocol

from .domain import Message
from .mime import parse_body

METADATA_HEADERS = ["From", "To", "Cc", "Date", "Subject", "Message-ID", "In-Reply-To",
	"References", "List-ID", "List-Unsubscribe", "Content-Type"]


class QuotaLimiter:
	"""A shared rolling-minute quota budget, including every multipart part.

	Google's May 2026 schedule assigns metadata/full gets 20/60 units and
	limits new projects to 6,000 units per user per minute. Older projects can
	have grandfathered limits; this conservative default works for either.
	https://developers.google.com/workspace/gmail/api/reference/quota
	"""

	def __init__(self, units_per_minute=6000, *, clock=time.monotonic, sleep=time.sleep, wait=None):
		self.limit = max(1, int(units_per_minute))
		self.clock = clock
		self.sleep = sleep
		self.wait = wait or threading.Event().wait
		self._events: deque[tuple[float, int]] = deque()
		self._lock = threading.Lock()
		self.consumed = 0

	def configure(self, units_per_minute):
		"""Change the budget without erasing quota already spent by other workers."""
		with self._lock:
			self.limit = int(units_per_minute)

	def acquire(self, units, *, cancel=None):
		"""Wait for budget while allowing the owning task to stop promptly.

		A direct Event can wake this wait immediately; a callable can combine a
		worker's stop signal and the owner's shutdown signal. The injectable wait
		keeps fake-clock tests deterministic without real sleeps.
		"""
		units = int(units)
		if units <= 0:
			raise ValueError("A request quota must be positive")
		while True:
			if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
				raise GmailInterrupted()
			with self._lock:
				# A saved budget can be lowered while another worker is waiting.
				# Recheck capacity before pruning/reserving, on every iteration.
				if units > self.limit:
					raise QuotaCapacityChanged(units, self.limit)
				now = self.clock()
				while self._events and self._events[0][0] <= now - 60:
					self._events.popleft()
				used = sum(event[1] for event in self._events)
				if used + units <= self.limit:
					self._events.append((now, units))
					self.consumed += units
					return
				delay = max(0.001, 60 - (now - self._events[0][0]))
			if cancel is None:
				self.sleep(delay)
			else:
				wait = getattr(cancel, "wait", self.wait)
				wait(min(delay, 0.1))


class GmailError(RuntimeError):
	def __init__(self, kind: str, message: str, retry_after: float | None = None,
			status: int | None = None, reason: str | None = None, *, sent: bool | None = None) -> None:
		super().__init__(message)
		self.kind = kind
		self.safe_message = message
		self.retry_after = retry_after
		self.status = status
		self.reason = reason
		# None means transport completion is unknown. False is reserved for a
		# failure proven to precede request.execute(), never a lost response.
		self.sent = sent
		self.transient = kind in {"network", "transient", "rate_limit"}


class PreSendError(GmailError):
	"""The mutation was not sent; no uncertain-write reconciliation is needed."""

	def __init__(self, error: GmailError) -> None:
		super().__init__(error.kind, error.safe_message, error.retry_after,
			status=error.status, reason=error.reason, sent=False)


class GmailInterrupted(PreSendError):
	"""A local stop/shutdown cancelled a request before it was sent."""

	def __init__(self) -> None:
		super().__init__(GmailError("interrupted", "Gmail quota wait was interrupted; the job can resume safely."))


class QuotaCapacityChanged(PreSendError):
	"""A changed budget cannot admit this request; nothing was reserved or sent."""

	def __init__(self, units: int, limit: int) -> None:
		super().__init__(GmailError("transient", "The quota budget changed before dispatch; split or retry the unsent request.", reason="quota_capacity_changed"))
		self.units = units
		self.limit = limit


class HistoryExpired(GmailError):
	def __init__(self) -> None:
		super().__init__("history_expired", "Gmail history expired; resumable inbox reconciliation is required.")


class PageTokenExpired(GmailError):
	def __init__(self) -> None:
		super().__init__("page_expired", "The Gmail page token expired; safely restart enumeration and deduplicate IDs.")


class Mailbox(Protocol):
	def identity(self) -> str: ...
	def labels(self) -> list[dict]: ...
	def profile(self) -> dict: ...
	def list_messages(self, query: str, page_token: str | None = None, page_size: int = 500) -> dict: ...
	def get_message(self, message_id: str, body: bool = False) -> Message: ...
	def get_messages(self, ids: list[str], body: bool = False) -> dict[str, Message | Exception]: ...
	def thread_metadata(self, thread_id: str) -> list[Message]: ...
	def history(self, start: str, page_token: str | None = None) -> dict: ...
	def modify(self, ids: list[str], add: list[str], remove: list[str]) -> None: ...


def translate_error(exc: Exception, operation: str = "request", has_page: bool = False) -> GmailError:
	if isinstance(exc, GmailError):
		return exc
	response = getattr(exc, "resp", None)
	status = int(getattr(response, "status", 0) or 0)
	reasons: list[str] = []
	try:
		payload = json.loads(getattr(exc, "content", b"{}"))
		reasons = [str(item.get("reason", "")) for item in payload.get("error", {}).get("errors", [])]
	except (ValueError, TypeError, AttributeError):
		reasons = []
		payload = {}
	retry_after = None
	if response is not None:
		try:
			value = response.get("retry-after")
			if value:
				try:
					retry_after = max(0.0, float(value))
				except ValueError:
					retry_after = max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
		except (ValueError, TypeError, AttributeError):
			retry_after = None
	if operation == "history" and status == 404:
		return HistoryExpired()
	if operation in {"list", "history"} and has_page and status == 400:
		# A malformed query with a page token is still a data error. Only the
		# provider's token-specific evidence permits restarting enumeration.
		message = str(payload.get("error", {}).get("message", "")).casefold() if isinstance(payload, dict) else ""
		if any(reason in {"invalidPageToken", "pageTokenExpired"} for reason in reasons) or ("pagetoken" in message.replace(" ", "").replace("_", "") and any(word in message for word in ("invalid", "expired"))):
			return PageTokenExpired()
	if status == 401:
		return GmailError("auth", "Gmail authorization is invalid. Reconnect the account.", status=status)
	if status == 403:
		if any(reason in {"rateLimitExceeded", "userRateLimitExceeded"} for reason in reasons):
			return GmailError("rate_limit", "Gmail rate limit reached.", retry_after, status=status, reason="rateLimitExceeded")
		if any(reason in {"dailyLimitExceeded", "quotaExceeded"} for reason in reasons):
			return GmailError("quota", "Gmail quota is exhausted. Pause and retry after the quota resets.", retry_after, status=status)
		return GmailError("permission", "Gmail denied permission. Check consent scopes and account policy.", status=status)
	if status == 404:
		return GmailError("missing", "The Gmail message or label no longer exists.", status=status)
	if status == 429:
		return GmailError("rate_limit", "Gmail rate limit reached.", retry_after, status=status)
	if status >= 500:
		return GmailError("transient", "Gmail is temporarily unavailable.", retry_after, status=status)
	if status:
		return GmailError("data", f"Gmail rejected the request (HTTP {status}).", status=status, reason=reasons[0] if reasons else None)
	try:
		from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
		from requests.exceptions import RequestException
		from httplib2 import HttpLib2Error
		# TransportError is also a GoogleAuthError. Classify transport failures
		# first so a temporary outage never asks the user to reconnect consent.
		if isinstance(exc, (TransportError, RequestException, HttpLib2Error, ssl.SSLError, OSError, TimeoutError)):
			return GmailError("network", "Gmail request failed. Check the network and retry.")
		if isinstance(exc, RefreshError):
			invalid_grant = any((isinstance(value, dict) and value.get("error") == "invalid_grant")
				or (isinstance(value, str) and "invalid_grant" in value) for value in exc.args)
			if not invalid_grant:
				return GmailError("transient", "Google authorization refresh is temporarily unavailable. Retry shortly.")
		if isinstance(exc, GoogleAuthError):
			return GmailError("auth", "Google credentials could not be refreshed. Reconnect the account.")
	except ImportError:
		# Google libraries are optional for the offline demo.
		return GmailError("network", "Gmail request failed. Check the network and retry.")
	return GmailError("network", "Gmail request failed. Check the network and retry.")


class GmailMailbox:
	is_demo = False

	def __init__(self, service: Any, account_id: str | None = None,
			retrieval_batch_size: int = 25, execution_batch_size: int = 100,
			body_limit: int = 32_000, quota_limiter: QuotaLimiter | None = None) -> None:
		self.service = service
		self.account_id = account_id
		self.retrieval_batch_size = max(1, min(50, int(retrieval_batch_size)))
		self.execution_batch_size = max(1, min(500, int(execution_batch_size)))
		self.body_limit = max(128, int(body_limit))
		self._catalog: list[dict] | None = None
		self.quota_limiter = quota_limiter or QuotaLimiter()
		self._sleep = time.sleep
		self._random = random.random  # noqa: S311 -- retry jitter is not security-sensitive randomness.
		self.on_label_error: Callable[[], None] | None = None
		self.cancelled: Callable[[], bool] | threading.Event | None = None

	def _acquire(self, units: int) -> None:
		if self.cancelled is None:
			self.quota_limiter.acquire(units)
		else:
			self.quota_limiter.acquire(units, cancel=self.cancelled)

	def _execute(self, request: Any, operation: str = "request", has_page: bool = False,
			*, idempotent: bool = True, quota_units: int = 1) -> dict:
		for attempt in range(3 if idempotent else 1):
			try:
				self._acquire(quota_units)
			except PreSendError:
				raise
			except Exception as exc:  # noqa: BLE001 - quota failures occur before the transport has sent a request
				raise PreSendError(translate_error(exc)) from None
			try:
				return request.execute(num_retries=0) or {}
			except Exception as exc:  # noqa: BLE001 -- transport/decode boundaries normalize arbitrary integration exceptions.
				error = translate_error(exc, operation, has_page)
				if not idempotent or not error.transient or attempt >= 2 or (error.retry_after or 0) > 60:
					raise error from None
				# Bound local retries. Longer Retry-After intervals remain durable
				# job-level waits instead of blocking a transport worker indefinitely.
				delay = max(error.retry_after or 0, min(32, 2 ** attempt + self._random()))
				self._sleep(delay)
		raise AssertionError("Unreachable retry exhaustion")

	def profile(self) -> dict:
		profile = self._execute(self.service.users().getProfile(userId="me"))
		identity = str(profile.get("emailAddress", "")).strip().lower()
		if not identity:
			raise GmailError("data", "Gmail did not return an account identity.")
		if self.account_id and identity != self.account_id.lower():
			raise GmailError("account", "The authenticated account differs from this mailbox's recorded account.")
		self.account_id = identity
		return profile

	def identity(self) -> str:
		# Recheck on every execution preflight, including a refreshed OAuth token.
		return str(self.profile()["emailAddress"]).lower()

	def labels(self) -> list[dict]:
		result = self._execute(self.service.users().labels().list(userId="me"))
		self._catalog = [{"id": str(item["id"]), "name": str(item.get("name", item["id"])),
			"type": str(item.get("type", "system"))} for item in result.get("labels", [])]
		return list(self._catalog)

	def list_messages(self, query: str, page_token: str | None = None, page_size: int = 500) -> dict:
		params: dict[str, Any] = {"userId": "me", "q": query, "maxResults": max(1, min(500, page_size)),
			"includeSpamTrash": False}
		if page_token:
			params["pageToken"] = page_token
		return self._execute(self.service.users().messages().list(**params), "list", bool(page_token), quota_units=5)

	def _request(self, message_id: str, body: bool) -> Any:
		params: dict[str, Any] = {"userId": "me", "id": message_id, "format": "full" if body else "metadata"}
		if not body:
			params["metadataHeaders"] = METADATA_HEADERS
		return self.service.users().messages().get(**params)

	def _decode(self, item: dict, body: bool) -> Message:
		headers: dict[str, str] = {}
		for header in item.get("payload", {}).get("headers", []):
			name = str(header.get("name", "")).lower()
			value = str(header.get("value", ""))
			try:
				value = str(make_header(decode_header(value)))
			except (LookupError, UnicodeError, ValueError):
				value = value.replace("\x00", "")
			headers[name] = (headers[name] + "\n" if name in headers else "") + value
		try:
			internal_ms = int(item["internalDate"])
			if internal_ms < 0:
				raise ValueError("negative date")
		except (ValueError, TypeError, KeyError):
			raise GmailError("data", "Gmail message has no valid internalDate.") from None
		message_id = str(item["id"])
		parsed = None
		if body:
			def fetch(attachment_id: str) -> dict:
				return self._execute(self.service.users().messages().attachments().get(
					userId="me", messageId=message_id, id=attachment_id), quota_units=20)
			parsed = parse_body(item.get("payload", {}), fetch, max_chars=self.body_limit)
		return Message(id=message_id, thread_id=str(item.get("threadId", message_id)),
			internal_ms=internal_ms, labels=set(item.get("labelIds", [])), headers=headers,
			snippet=str(item.get("snippet", "")), body=parsed.text if parsed else None,
			truncated=parsed.truncated if parsed else False,
			missing_content=parsed.missing if parsed else True, account_id=self.account_id or "",
			attachments=parsed.attachments if parsed else [], content_flags=parsed.content_flags if parsed else [])

	def get_message(self, message_id: str, body: bool = False) -> Message:
		return self._decode(self._execute(self._request(message_id, body), quota_units=60 if body else 20), body)

	def get_messages(self, ids: list[str], body: bool = False) -> dict[str, Message | Exception]:
		"""HTTP multipart batches have per-part results; transport success is not item success."""
		ids = list(dict.fromkeys(ids))
		results: dict[str, Message | Exception] = {}
		units = 60 if body else 20
		offset = 0
		while offset < len(ids):
			batch_size = min(self.retrieval_batch_size, max(1, self.quota_limiter.limit // units))
			batch_ids = ids[offset:offset + batch_size]
			raw: dict[str, dict | Exception] = {}
			def callback(request_id: str, response: dict | None, exception: Exception | None, raw=raw) -> None:
				raw[request_id] = translate_error(exception) if exception is not None else (response or {})
			batch = self.service.new_batch_http_request(callback=callback)
			for message_id in batch_ids:
				batch.add(self._request(message_id, body), request_id=message_id)
			try:
				self._acquire(len(batch_ids) * units)
				batch.execute()
			except QuotaCapacityChanged as exc:
				if len(batch_ids) > 1 and exc.limit >= units:
					# No part reached the wire. Rebuild only this unsent suffix
					# against the latest capacity; completed parts are never replayed.
					continue
				raw.update({message_id: exc for message_id in batch_ids})
			except Exception as exc:  # noqa: BLE001 -- transport/decode boundaries normalize arbitrary integration exceptions.
				for message_id in batch_ids:
					if message_id not in raw:
						raw[message_id] = translate_error(exc)
			for message_id in batch_ids:
				value = raw.get(message_id, GmailError("transient", "Gmail batch returned no result for this message."))
				if isinstance(value, Exception):
					results[message_id] = value
				else:
					try:
						results[message_id] = self._decode(value, body)
					except Exception as exc:  # noqa: BLE001 -- transport/decode boundaries normalize arbitrary integration exceptions.
						results[message_id] = translate_error(exc)
			offset += len(batch_ids)
		return results

	def thread_metadata(self, thread_id: str) -> list[Message]:
		# metadata format never fetches bodies of excluded/recent replies.
		result = self._execute(self.service.users().threads().get(userId="me", id=thread_id,
			format="metadata", metadataHeaders=METADATA_HEADERS), quota_units=40)
		return [self._decode(item, False) for item in result.get("messages", [])]

	def history(self, start: str, page_token: str | None = None) -> dict:
		params: dict[str, Any] = {"userId": "me", "startHistoryId": start, "maxResults": 500}
		if page_token:
			params["pageToken"] = page_token
		# Do not filter labelId=INBOX: doing so can lose removal events.
		return self._execute(self.service.users().history().list(**params), "history", bool(page_token), quota_units=2)

	def validate_labels(self, add: list[str], remove: list[str], *, refresh: bool = False) -> None:
		"""Resolve and validate the live catalog before a write attempt is created."""
		try:
			catalog = self.labels() if refresh or self._catalog is None else self._catalog
		except PreSendError:
			raise
		except Exception as exc:  # noqa: BLE001 - catalog retrieval is read-only and precedes the mutation
			raise PreSendError(translate_error(exc)) from None
		allowed = {item["id"] for item in catalog if item["type"] == "user"} | {"INBOX"}
		if not set(add).issubset(allowed) or not set(remove).issubset(allowed) or set(add) & set(remove):
			self._catalog = None
			if self.on_label_error is not None:
				self.on_label_error()
			raise PreSendError(GmailError("permission", "Only INBOX and existing user-label deltas are allowed."))

	def modify(self, ids: list[str], add: list[str], remove: list[str]) -> None:
		"""Apply label deltas only. Callers ledger/preflight/reconcile each explicit ID."""
		ids = list(dict.fromkeys(ids))
		if not ids:
			return
		self.validate_labels(add, remove)
		for offset in range(0, len(ids), self.execution_batch_size):
			try:
				try:
					request = self.service.users().messages().batchModify(userId="me", body={
					"ids": ids[offset:offset + self.execution_batch_size],
					"addLabelIds": sorted(set(add)), "removeLabelIds": sorted(set(remove))})
				except Exception as exc:  # noqa: BLE001 - constructing the request cannot have sent its body
					raise PreSendError(translate_error(exc)) from None
				self._execute(request,
					"modify", idempotent=False, quota_units=50)
			except GmailError as exc:
				if offset and exc.sent is False:
					# A multi-chunk caller may already have committed earlier chunks.
					# Only the usual per-chunk caller can safely claim nothing was sent.
					raise GmailError(exc.kind, exc.safe_message, exc.retry_after,
						status=exc.status, reason=exc.reason) from None
				if exc.status in {400, 404}:
					self._catalog = None
					if self.on_label_error is not None:
						self.on_label_error()
				raise
		# batchModify has an empty response; the application must verify each effect.
