"""Deterministic synthetic AI contract; no network, keys, or actual provider calls.

Demo-only models: demo-default, demo-uncertain, demo-malformed, demo-missing-key,
demo-rate-limit, demo-empty, demo-delayed, demo-provider-error. The delayed model
waits 0.25 seconds so a local watchdog can be exercised without a remote request.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import time

DEFAULT_MODEL = "demo-default"


class AIAPIsError(Exception):
	"""Base exception for the synthetic demo module."""


class UnsupportedModelError(AIAPIsError):
	"""Raised for an unknown demo-only model."""


class MissingAPIKeyError(AIAPIsError):
	"""Synthetic configuration error; this module never requires a real key."""


class APIResponseError(AIAPIsError):
	"""Synthetic invalid provider response."""


class RateLimitError(AIAPIsError):
	"""Synthetic rate limit."""


@dataclass(frozen=True)
class ModelResult:
	"""The optional metadata surface of the external module's contract."""
	text: str
	model: str
	provider: str
	duration_s: float


def _payload(prompt: str) -> dict:
	start = "UNTRUSTED_JSON_DATA_START\n"
	end = "\nUNTRUSTED_JSON_DATA_END"
	if start in prompt and end in prompt:
		return json.loads(prompt.split(start, 1)[1].rsplit(end, 1)[0])
	try:
		return json.loads(prompt)
	except (ValueError, TypeError):
		return {"messages": []}


def _assessment(message: dict, labels: list[dict], uncertain: bool) -> dict:
	"""Synthetic scenarios are assertions in generated data, not production rules."""
	headers = {k.lower(): str(v) for k, v in message.get("headers", {}).items()}
	subject = headers.get("subject", "").casefold()
	body = str(message.get("body", "")).casefold()
	text = subject + " " + body
	category = "other"
	importance = "medium"
	action = "none"
	score = 20
	reason = "Synthetic routine message has no stated outstanding task."
	if any(term in text for term in ("newsletter", "digest", "edition", "list-unsubscribe")) or headers.get("list-id"):
		category, importance, score = "newsletter", "low", 5
		reason = "Synthetic recurring newsletter contains no request for action."
	if any(term in text for term in ("promotion", "sale ended", "offer expired", "limited offer", "coupon", "discount")):
		category, importance, score = "promotion", "low", 3
		reason = "Synthetic promotional material has no outstanding task."
	if any(term in text for term in ("receipt", "paid invoice", "order confirmation", "payment received")):
		category, importance, score = "receipt", "medium", 10
		reason = "Synthetic receipt records a completed payment."
	if any(term in text for term in ("financial", "insurance", "bank statement", "tax record")):
		category, importance, score = "financial", "high", 18
		reason = "Synthetic financial record explicitly states completion."
	if any(term in text for term in ("medical", "clinical", "patient record", "follow-up question")):
		category, importance, score = "medical", "high", 18
		reason = "Synthetic medical record is retained for reference."
	if any(term in text for term in ("legal", "signed agreement")):
		category, importance, score = "legal", "high", 20
	if any(term in text for term in ("event", "webinar", "conference concluded", "registration ended")):
		category, importance, score = "event", "low", 7
		reason = "Synthetic past event notice has no stated outstanding task."
	if any(term in text for term in ("notification", "delivery complete", "build succeeded")):
		category, importance, score = "notification", "low", 8
	if "project completed" in text:
		category, importance, score = "work_request", "high", 15
		reason = "Synthetic project correspondence explicitly establishes completion."
	if any(term in text for term in ("please reply", "reply needed", "awaiting your response", "outstanding request", "needs your approval", "task pending", "please approve")):
		category, importance, action, score = "work_request", "high", "reply_needed", 92
		reason = "Synthetic message explicitly requests a response that is not established as complete."
	if any(term in text for term in ("completion uncertain", "status unclear", "missing context", "ambiguous request")):
		uncertain, action, score = True, "uncertain", 55
		reason = "Synthetic message lacks evidence establishing completion."
	missing = bool(message.get("body_truncated")) or bool(message.get("missing_content")) or not body.strip()
	if missing:
		uncertain = True
		reason = "Synthetic message content is incomplete; completion cannot be established."
	suggested = []
	for label in labels:
		name = str(label.get("name", "")).casefold()
		if category == "newsletter" and "newsletter" in name or category in {"receipt", "financial"} and any(term in name for term in ("receipt", "finance", "financial")) or category == "medical" and "medical" in name:
			suggested.append(label["id"])
	return {"message_id": message["message_id"], "category": category, "suggested_label_ids": suggested[:2], "importance": importance, "outstanding_action": action, "urgency": "medium" if action != "none" else "none", "deadline_utc": None, "keep_in_inbox_score": score, "uncertainty": "high" if uncertain else "low", "missing_context": missing, "evidence_reason": reason}


def call_api(prompt: str, model: str = DEFAULT_MODEL) -> str:
	"""Return synthetic response text using the exact blocking public contract."""
	return call_api_result(prompt=prompt, model=model).text


def call_api_result(prompt: str, model: str = DEFAULT_MODEL) -> ModelResult:
	"""Return synthetic text and metadata without importing any provider SDK."""
	started = time.monotonic()
	models = {"demo-default", "demo-uncertain", "demo-malformed", "demo-missing-key", "demo-rate-limit", "demo-empty", "demo-delayed", "demo-provider-error"}
	if model not in models:
		raise UnsupportedModelError("Unknown demo-only model")
	if model == "demo-missing-key":
		raise MissingAPIKeyError("Synthetic missing-key scenario")
	if model == "demo-rate-limit":
		raise RateLimitError("Synthetic rate-limit scenario")
	if model == "demo-provider-error":
		raise RuntimeError("Synthetic unrecognized provider failure")
	if model == "demo-empty":
		raise APIResponseError("Synthetic missing provider content")
	if model == "demo-delayed":
		time.sleep(0.25)
	if model == "demo-malformed":
		text = "```json\n{not valid JSON}\n```"
	else:
		payload = _payload(prompt)
		assessments = [_assessment(message, payload.get("existing_user_labels", []), model == "demo-uncertain") for message in payload.get("messages", [])]
		text = json.dumps({"schema_version": "1.0", "assessments": assessments}, ensure_ascii=False)
	return ModelResult(text=text, model=model, provider="offline-synthetic-demo", duration_s=time.monotonic() - started)
