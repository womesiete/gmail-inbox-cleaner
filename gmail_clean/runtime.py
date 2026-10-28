"""Small shared runtime helpers; no mailbox content is written to diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
	return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
	return (value or utc_now()).astimezone(timezone.utc).isoformat()


def encode(value: Any) -> str:
	return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
	return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def identifier() -> str:
	return uuid.uuid4().hex


def data_directory(demo: bool = False) -> Path:
	base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
	return base / "GmailInboxCleaner" / ("demo" if demo else "live")


def safe_error(exc: BaseException) -> str:
	"""Only explicitly sanitized integration summaries cross into the error queue."""
	if type(exc).__module__.startswith("gmail_clean."):
		value = getattr(exc, "summary", None) or getattr(exc, "safe_message", None)
		if value:
			return str(value)[:500]
	return f"{type(exc).__module__}.{type(exc).__name__}: operation failed; inspect configuration or retry"


def redact(text: str) -> str:
	text = re.sub(r"(?i)(bearer\s+|(?:access_token|refresh_token|client_secret|api[_-]?key)[\s\"':=]+)[^\s,\"'}]+", r"\1[REDACTED]", text)
	return re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[address]", text)[:1000]


def insert(store: Any, table: str, values: dict[str, Any], *, replace: bool = False) -> None:
	"""Internal column names only; all values remain bound SQL parameters."""
	columns = ",".join(values)
	marks = ",".join("?" for _ in values)
	verb = "INSERT OR REPLACE" if replace else "INSERT"
	store.execute(f"{verb} INTO {table} ({columns}) VALUES ({marks})", tuple(values.values()))
