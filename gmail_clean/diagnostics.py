"""Bounded operational logs; diagnostic bundles never read mailbox databases."""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import threading
import zipfile

from .runtime import redact

_LOCK = threading.RLock()


class _RedactedFormatter(logging.Formatter):
	def format(self, record):
		return redact(super().format(record))


def configure(directory: Path, level: str = "INFO") -> logging.Logger:
	"""Reuse one thread-safe rotating handler for all workers in a directory."""
	path = directory.resolve() / "logs" / "application.log"
	with _LOCK:
		logger = logging.getLogger("gmail_clean." + str(path))
		logger.setLevel(getattr(logging, level.upper(), logging.INFO))
		logger.propagate = False
		if not logger.handlers:
			path.parent.mkdir(parents=True, exist_ok=True)
			handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8", delay=True)
			handler.setFormatter(_RedactedFormatter("%(asctime)s %(levelname)s %(message)s"))
			logger.addHandler(handler)
		return logger


def event(logger: logging.Logger, name: str, *, job_id: str = "", stage: str = "", kind: str = "", exception: str = "", level=logging.INFO):
	"""Accept structural identifiers only, never exceptions, subjects or body text."""
	def token(value):
		return re.sub(r"[^A-Za-z0-9_.:-]", "_", str(value))[:80]
	logger.log(level, "event=%s job=%s stage=%s kind=%s exception=%s", *(token(value) for value in (name, job_id, stage, kind, exception)))


def export(directory: Path, destination: Path, *, schema_version: int) -> str:
	"""Export safe software metadata and bounded logs, excluding settings and mail."""
	with _LOCK:
		logger = logging.getLogger("gmail_clean." + str(directory.resolve() / "logs" / "application.log"))
		for handler in logger.handlers:
			handler.flush()
		destination.parent.mkdir(parents=True, exist_ok=True)
		with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
			bundle.writestr("manifest.json", json.dumps({"application": "gmail-inbox-cleaner", "schema_version": schema_version, "contains": "operational metadata only"}, indent=2))
			for path in sorted((directory / "logs").glob("application.log*")):
				if path.is_file() and path.stat().st_size <= 1_100_000:
					# Re-redact on export as defense in depth against older log formats.
					text = "\n".join(redact(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines())
					bundle.writestr("logs/" + path.name, text)
	return str(destination)


def close(logger: logging.Logger) -> None:
	"""Release open log files when the application owner exits (also on Windows)."""
	with _LOCK:
		for handler in list(logger.handlers):
			handler.close()
			logger.removeHandler(handler)
