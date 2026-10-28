"""Message-scoped Gmail transport. No thread-wide or destructive write methods."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from .domain import Message
from .mime import parse_body

METADATA_HEADERS = ["From", "To", "Cc", "Date", "Subject", "Message-ID", "In-Reply-To",
	"References", "List-ID", "List-Unsubscribe", "Content-Type"]


class GmailError(RuntimeError):
	def __init__(self, kind: str, message: str, retry_after: float | None = None,
			status: int | None = None, reason: str | None = None) -> None:
		super().__init__(message)
		self.kind = kind
		self.safe_message = message
		self.retry_after = retry_after
		self.status = status
		self.reason = reason
		self.transient = kind in {"network", "transient", "rate_limit"}


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
		return GmailError("data", f"Gmail rejected the request (HTTP {status}).", status=status)
	try:
		from google.auth.exceptions import GoogleAuthError
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
			body_limit: int = 32_000) -> None:
		self.service = service
		self.account_id = account_id
		self.retrieval_batch_size = max(1, min(50, int(retrieval_batch_size)))
		self.execution_batch_size = max(1, min(500, int(execution_batch_size)))
		self.body_limit = max(128, int(body_limit))
		self._catalog: list[dict] | None = None

	def _execute(self, request: Any, operation: str = "request", has_page: bool = False) -> dict:
		try:
			return request.execute(num_retries=0) or {}
		except Exception as exc:
			raise translate_error(exc, operation, has_page) from None

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
		return self._execute(self.service.users().messages().list(**params), "list", bool(page_token))

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
					userId="me", messageId=message_id, id=attachment_id))
			parsed = parse_body(item.get("payload", {}), fetch, max_chars=self.body_limit)
		return Message(id=message_id, thread_id=str(item.get("threadId", message_id)),
			internal_ms=internal_ms, labels=set(item.get("labelIds", [])), headers=headers,
			snippet=str(item.get("snippet", "")), body=parsed.text if parsed else None,
			truncated=parsed.truncated if parsed else False,
			missing_content=parsed.missing if parsed else True, account_id=self.account_id or "",
			attachments=parsed.attachments if parsed else [])

	def get_message(self, message_id: str, body: bool = False) -> Message:
		return self._decode(self._execute(self._request(message_id, body)), body)

	def get_messages(self, ids: list[str], body: bool = False) -> dict[str, Message | Exception]:
		"""HTTP multipart batches have per-part results; transport success is not item success."""
		ids = list(dict.fromkeys(ids))
		results: dict[str, Message | Exception] = {}
		for offset in range(0, len(ids), self.retrieval_batch_size):
			batch_ids = ids[offset:offset + self.retrieval_batch_size]
			raw: dict[str, dict | Exception] = {}
			def callback(request_id: str, response: dict | None, exception: Exception | None) -> None:
				raw[request_id] = translate_error(exception) if exception is not None else (response or {})
			batch = self.service.new_batch_http_request(callback=callback)
			for message_id in batch_ids:
				batch.add(self._request(message_id, body), request_id=message_id)
			try:
				batch.execute()
			except Exception as exc:
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
					except Exception as exc:
						results[message_id] = translate_error(exc)
		return results

	def thread_metadata(self, thread_id: str) -> list[Message]:
		# metadata format never fetches bodies of excluded/recent replies.
		result = self._execute(self.service.users().threads().get(userId="me", id=thread_id,
			format="metadata", metadataHeaders=METADATA_HEADERS))
		return [self._decode(item, False) for item in result.get("messages", [])]

	def history(self, start: str, page_token: str | None = None) -> dict:
		params: dict[str, Any] = {"userId": "me", "startHistoryId": start, "maxResults": 500}
		if page_token:
			params["pageToken"] = page_token
		# Do not filter labelId=INBOX: doing so can lose removal events.
		return self._execute(self.service.users().history().list(**params), "history", bool(page_token))

	def modify(self, ids: list[str], add: list[str], remove: list[str]) -> None:
		"""Apply label deltas only. Callers ledger/preflight/reconcile each explicit ID."""
		ids = list(dict.fromkeys(ids))
		if not ids:
			return
		catalog = self._catalog if self._catalog is not None else self.labels()
		allowed = {item["id"] for item in catalog if item["type"] == "user"} | {"INBOX"}
		if not set(add).issubset(allowed) or not set(remove).issubset(allowed) or set(add) & set(remove):
			raise GmailError("permission", "Only INBOX and existing user-label deltas are allowed.")
		for offset in range(0, len(ids), self.execution_batch_size):
			self._execute(self.service.users().messages().batchModify(userId="me", body={
				"ids": ids[offset:offset + self.execution_batch_size],
				"addLabelIds": sorted(set(add)), "removeLabelIds": sorted(set(remove))}))
		# batchModify has an empty response; the application must verify each effect.
