"""Pure mailbox models, frozen date windows, hashes, and deterministic policy.

No Gmail, Qt, or database dependencies belong in this module. Gmail internalDate
is the only source of message age; all operational instants are aware UTC values.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime, timedelta
from email.utils import parseaddr
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CATEGORIES = frozenset({"newsletter", "promotion", "notification", "receipt", "financial", "medical", "legal", "personal", "work_request", "event", "other"})
SYSTEM_EXCLUSIONS = frozenset({"SPAM", "TRASH", "DRAFT"})
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def utc_now() -> datetime:
	return datetime.now(UTC)


def as_utc(value: datetime | str) -> datetime:
	"""Reject naive instants rather than guessing the host's local timezone."""
	if isinstance(value, str):
		value = datetime.fromisoformat(value.replace("Z", "+00:00"))
	if value.tzinfo is None or value.utcoffset() is None:
		raise ValueError("An explicit timezone is required for an instant")
	return value.astimezone(UTC)


def iso_utc(value: datetime | str) -> str:
	return as_utc(value).isoformat().replace("+00:00", "Z")


def epoch_ms(value: datetime) -> int:
	return (as_utc(value) - EPOCH) // timedelta(milliseconds=1)


def from_epoch_ms(value: int) -> datetime:
	if isinstance(value, bool) or not isinstance(value, int):
		raise ValueError("internalDate must be integer epoch milliseconds")
	return EPOCH + timedelta(milliseconds=value)


def canonical_hash(value: Any) -> str:
	return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def system_timezone() -> str:
	"""Resolve the host's IANA zone, including Windows registry zone mappings."""
	try:
		from tzlocal import get_localzone_name
		name = os.environ.get("TZ", "").removeprefix(":") or get_localzone_name()
		ZoneInfo(name)
		return name
	except (ImportError, ValueError, OSError, ZoneInfoNotFoundError):
		# UTC is explicit and stable if the OS timezone is not configured.
		return "UTC"


@dataclass
class Config:
	"""Serializable preferences. Secrets and operational state are never settings.

	Category overrides use {category: {mode: 'inherit'|'disabled'|'custom',
	months: 2, days: 0}}. A list of incompatible overrides is treated as a
	configuration conflict rather than arbitrarily selecting one.
	"""

	timezone: str = field(default_factory=system_timezone)
	earliest_date: str | None = None
	latest_mode: str = "rolling"
	latest_days: int = 14
	latest_date: str | None = None
	age_enabled: bool = True
	age_months: int = 2
	age_days: int = 0
	category_age_overrides: dict[str, Any] = field(default_factory=dict)
	archive_threshold: int = 25
	keep_threshold: int = 70
	age_archive_max_score: int = 65
	protect_starred: bool = True
	protected_senders: list[str] = field(default_factory=list)
	protected_domains: list[str] = field(default_factory=list)
	protected_label_ids: list[str] = field(default_factory=list)
	label_meanings: dict[str, str] = field(default_factory=dict)
	dry_run: bool = True
	auto_execute: bool = False
	auto_label: bool = False
	learning_enabled: bool = True
	ai_module: str = ""
	ai_module_sha256: str = ""
	ai_model: str = ""
	ai_use_call_api_result: bool = False
	ai_batch_size: int = 1
	ai_timeout_seconds: float = 120.0
	retrieval_batch_size: int = 25
	execution_batch_size: int = 100
	max_actions_per_run: int = 1000
	max_retries: int = 3
	retry_base_seconds: float = 2.0
	maintenance_interval_minutes: int = 60
	reconciliation_interval_hours: int = 24
	download_all_bodies: bool = False
	body_limit: int = 32000
	sent_context_enabled: bool = False
	archived_context_enabled: bool = False
	excluded_senders: list[str] = field(default_factory=list)
	excluded_domains: list[str] = field(default_factory=list)
	excluded_label_ids: list[str] = field(default_factory=list)
	excluded_categories: list[str] = field(default_factory=list)
	retention_days: int = 180
	log_level: str = "INFO"
	shortcuts: dict[str, str] = field(default_factory=dict)
	optional_model_routes: dict[str, str] = field(default_factory=dict)
	estimated_cost_per_call: float = 0.0
	max_ai_calls_per_job: int = 5000
	max_ai_calls_per_day: int = 10000
	max_ai_cost_per_job: float = 0.0
	max_ai_cost_per_day: float = 0.0
	extras: dict[str, Any] = field(default_factory=dict)

	@classmethod
	def from_dict(cls, value: dict[str, Any]) -> Config:
		known = {item.name for item in fields(cls)}
		values = {key: data for key, data in value.items() if key in known}
		values["extras"] = {**value.get("extras", {}), **{key: data for key, data in value.items() if key not in known}}
		return cls(**values)

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)

	@property
	def revision(self) -> str:
		"""Execution-relevant revision; layout/logging changes do not stale plans."""
		value = self.to_dict()
		for key in ("dry_run", "max_actions_per_run", "shortcuts", "log_level", "maintenance_interval_minutes", "retention_days", "estimated_cost_per_call", "extras", "max_ai_calls_per_job", "max_ai_calls_per_day", "max_ai_cost_per_job", "max_ai_cost_per_day"):
			value.pop(key, None)
		return canonical_hash(value)

	def validate(self) -> None:
		if self.ai_module_sha256 and not re.fullmatch(r"[0-9a-f]{64}", self.ai_module_sha256):
			raise ValueError("AI module SHA-256 must be a lowercase 64-character fingerprint")
		try:
			ZoneInfo(self.timezone)
		except (ZoneInfoNotFoundError, TypeError) as exc:
			raise ValueError("Choose a valid IANA policy timezone") from exc
		if self.latest_mode not in {"rolling", "fixed"}:
			raise ValueError("Latest mode must be rolling or fixed")
		for name in ("latest_days", "age_months", "age_days", "max_retries", "retention_days", "max_actions_per_run", "max_ai_calls_per_job", "max_ai_calls_per_day"):
			if type(getattr(self, name)) is not int or getattr(self, name) < 0:
				raise ValueError(f"{name} must be a non-negative integer")
		for name in ("estimated_cost_per_call", "max_ai_cost_per_job", "max_ai_cost_per_day"):
			value = getattr(self, name)
			if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
				raise ValueError(f"{name} must be a finite non-negative amount")
		if not 0 <= self.archive_threshold < self.keep_threshold <= 100:
			raise ValueError("Require 0 <= archive threshold < keep threshold <= 100")
		if not 0 <= self.age_archive_max_score <= 100:
			raise ValueError("Age archive maximum score must be 0–100")
		for name, low, high in (("ai_batch_size", 1, 10), ("retrieval_batch_size", 1, 50), ("execution_batch_size", 1, 500)):
			if type(getattr(self, name)) is not int or not low <= getattr(self, name) <= high:
				raise ValueError(f"{name} must be {low}–{high}")
		if self.body_limit < 128 or self.ai_timeout_seconds <= 0 or self.retry_base_seconds <= 0:
			raise ValueError("Body limit must be >=128 and retry/watchdog intervals must be positive")
		if self.earliest_date:
			_parse_date(self.earliest_date)
		if self.latest_mode == "fixed":
			if not self.latest_date:
				raise ValueError("A fixed latest calendar date is required")
			_parse_date(self.latest_date)
		for category in self.category_age_overrides:
			if category not in CATEGORIES:
				raise ValueError(f"Unknown category age override: {category}")
			try:
				_age_spec(self, category)
			except PolicyConflict:
				# Conflicting applicable overrides are a per-item review reason;
				# keeping them visible must not prevent the entire job from opening.
				continue


@dataclass
class Message:
	id: str
	thread_id: str
	internal_ms: int
	labels: set[str] = field(default_factory=set)
	headers: dict[str, str] = field(default_factory=dict)
	snippet: str = ""
	body: str | None = None
	truncated: bool = False
	missing_content: bool = False
	content_hash: str = ""
	context_version: str = ""
	account_id: str = ""
	attachments: list[dict[str, Any]] = field(default_factory=list)
	content_flags: list[str] = field(default_factory=list)

	@property
	def sender(self) -> str:
		return normalize_sender(self.header("From"))

	@property
	def subject(self) -> str:
		return self.header("Subject")

	@property
	def internal_date(self) -> datetime:
		return from_epoch_ms(self.internal_ms)

	def header(self, name: str) -> str:
		return next((value for key, value in self.headers.items() if key.casefold() == name.casefold()), "")

	def to_dict(self) -> dict[str, Any]:
		value = asdict(self)
		value["labels"] = sorted(self.labels)
		return value

	@classmethod
	def from_dict(cls, value: dict[str, Any]) -> Message:
		known = {item.name for item in fields(cls)}
		values = {key: data for key, data in value.items() if key in known}
		values["labels"] = set(values.get("labels", []))
		return cls(**values)


@dataclass
class Assessment:
	message_id: str
	category: str
	suggested_label_ids: list[str]
	importance: str
	outstanding_action: str
	urgency: str
	deadline_utc: str | None
	keep_in_inbox_score: int
	uncertainty: str
	missing_context: bool
	evidence_reason: str

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)

	@classmethod
	def from_dict(cls, value: dict[str, Any]) -> Assessment:
		return cls(**value)


@dataclass(frozen=True)
class Decision:
	action: str
	reason: str
	label_ids: tuple[str, ...] = ()
	evidence_source: str = "policy"

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass(frozen=True)
class Window:
	anchor_utc: datetime
	earliest_inclusive: datetime | None
	latest_exclusive: datetime
	timezone: str = "America/New_York"

	def __post_init__(self) -> None:
		object.__setattr__(self, "anchor_utc", as_utc(self.anchor_utc))
		object.__setattr__(self, "latest_exclusive", as_utc(self.latest_exclusive))
		if self.earliest_inclusive is not None:
			object.__setattr__(self, "earliest_inclusive", as_utc(self.earliest_inclusive))
			if self.earliest_inclusive >= self.latest_exclusive:
				raise ValueError("Examination window is empty or inverted")

	def contains(self, internal_ms: int) -> bool:
		instant = from_epoch_ms(internal_ms)
		return (self.earliest_inclusive is None or instant >= self.earliest_inclusive) and instant < self.latest_exclusive

	@property
	def earliest_ms(self) -> int | None:
		return epoch_ms(self.earliest_inclusive) if self.earliest_inclusive else None

	@property
	def latest_ms(self) -> int:
		return epoch_ms(self.latest_exclusive)

	def to_dict(self) -> dict[str, Any]:
		return {"anchor_utc": iso_utc(self.anchor_utc), "earliest_inclusive": iso_utc(self.earliest_inclusive) if self.earliest_inclusive else None, "latest_exclusive": iso_utc(self.latest_exclusive), "timezone": self.timezone}

	@classmethod
	def from_dict(cls, value: dict[str, Any]) -> Window:
		return cls(as_utc(value["anchor_utc"]), as_utc(value["earliest_inclusive"]) if value.get("earliest_inclusive") else None, as_utc(value["latest_exclusive"]), value.get("timezone", "America/New_York"))

	def query(self, margin_seconds: int = 86400) -> str:
		"""Deliberately overfetch; exact internalDate filtering remains mandatory."""
		parts = ["in:inbox", "-in:spam", "-in:trash", "-is:draft"]
		if self.earliest_inclusive is not None:
			parts.append(f"after:{epoch_ms(self.earliest_inclusive) // 1000 - margin_seconds}")
		parts.append(f"before:{epoch_ms(self.latest_exclusive) // 1000 + margin_seconds + 1}")
		return " ".join(parts)

	def describe(self) -> str:
		zone = ZoneInfo(self.timezone)
		start = self.earliest_inclusive.astimezone(zone).isoformat() if self.earliest_inclusive else "unlimited"
		end = self.latest_exclusive.astimezone(zone).isoformat()
		return f"[{start}, {end}) — {self.timezone} (internalDate)"


def _parse_date(value: str) -> date:
	if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
		raise ValueError("Calendar dates must use YYYY-MM-DD")
	return date.fromisoformat(value)


def resolve_wall_time(value: datetime, zone: ZoneInfo) -> datetime:
	"""Use fold=0; advance nonexistent wall times by the DST transition gap.

	Round-tripping fold=0 through UTC moves a nonexistent 02:30 spring wall
	time to 03:30. Ambiguous fall wall times retain their first occurrence.
	"""
	wall = value.replace(tzinfo=None)
	aware = wall.replace(tzinfo=zone, fold=0)
	roundtrip = aware.astimezone(UTC).astimezone(zone)
	if roundtrip.replace(tzinfo=None) != wall:
		return roundtrip
	return aware


def resolve_window(config: Config, anchor: datetime | None = None) -> Window:
	config.validate()
	anchor_utc = as_utc(anchor or utc_now())
	zone = ZoneInfo(config.timezone)
	earliest = None
	if config.earliest_date:
		start = _parse_date(config.earliest_date)
		earliest = resolve_wall_time(datetime.combine(start, datetime.min.time()), zone).astimezone(UTC)
	if config.latest_mode == "fixed":
		day_after = _parse_date(config.latest_date or "") + timedelta(days=1)
		latest = resolve_wall_time(datetime.combine(day_after, datetime.min.time()), zone).astimezone(UTC)
	else:
		latest = anchor_utc - timedelta(days=config.latest_days)
	return Window(anchor_utc, earliest, latest, config.timezone)


def subtract_calendar_months(anchor: datetime, months: int, timezone: str = "America/New_York") -> datetime:
	if type(months) is not int or months < 0:
		raise ValueError("Calendar month count must be non-negative")
	zone = ZoneInfo(timezone)
	local = as_utc(anchor).astimezone(zone)
	index = local.year * 12 + local.month - 1 - months
	year, month_index = divmod(index, 12)
	if year < 1:
		raise ValueError("Calendar subtraction precedes year 1")
	month = month_index + 1
	day = min(local.day, calendar.monthrange(year, month)[1])
	wall = local.replace(year=year, month=month, day=day, tzinfo=None, fold=0)
	return resolve_wall_time(wall, zone).astimezone(UTC)


class PolicyConflict(ValueError):
	"""Several equally applicable policies require an explicit user resolution."""


def _age_spec(config: Config, category: str | None) -> tuple[bool, int, int]:
	value = config.category_age_overrides.get(category) if category else None
	if isinstance(value, list):
		unique = {canonical_hash(item): item for item in value}
		if len(unique) > 1:
			raise PolicyConflict("Conflicting applicable category age overrides")
		value = next(iter(unique.values()), None)
	if value is None or value == "inherit" or (isinstance(value, dict) and value.get("mode", "inherit") == "inherit"):
		return config.age_enabled, config.age_months, config.age_days
	if value == "disabled":
		return False, 0, 0
	if not isinstance(value, dict):
		raise ValueError("Category age override must be an object")
	mode = value.get("mode")
	if mode == "disabled":
		return False, 0, 0
	if mode != "custom":
		raise ValueError("Category override mode must be inherit, disabled, or custom")
	months, days = value.get("months", 0), value.get("days", 0)
	if type(months) is not int or type(days) is not int or min(months, days) < 0:
		raise ValueError("Category month/day ages must be non-negative integers")
	return True, months, days


def resolve_age_cutoff(config: Config, anchor: datetime, category: str | None = None) -> datetime | None:
	enabled, months, days = _age_spec(config, category)
	if not enabled:
		return None
	# Month arithmetic is wall-clock/calendar based; an explicit day threshold
	# denotes 24-hour days independently of the month clamping operation.
	return subtract_calendar_months(anchor, months, config.timezone) - timedelta(days=days)


def normalize_sender(value: str) -> str:
	return parseaddr(value)[1].strip().casefold()


def domain_matches(sender_or_domain: str, target_domain: str) -> bool:
	"""Match a domain and its subdomains without accepting suffix lookalikes."""
	value = str(sender_or_domain).casefold().strip()
	domain = value.rpartition("@")[2] if "@" in value else value.lstrip("@")
	target = str(target_domain).casefold().strip().lstrip("@")
	return bool(domain and target and (domain == target or domain.endswith("." + target)))


def normalize_text(value: str) -> str:
	return " ".join(value.replace("\x00", "").split())


_INSTRUCTION_LIKE = re.compile(
	r"\b(?:ignore|disregard|override)\b.{0,100}\b(?:previous|prior|above|system|developer|all)\b.{0,60}\b(?:instructions?|prompts?|rules?)\b"
	r"|\b(?:system|developer)\s+(?:message|prompt|instructions?)\b"
	r"|\b(?:return|output|set|mark|classify|assess)\b.{0,100}\b(?:keep_in_inbox_score|missing_context|outstanding_action|assessments?|message_id)\b"
	r"|\b(?:archive|label|classify|assess)\b.{0,80}\b(?:other|all|remaining|every)\s+(?:messages?|emails?)\b"
	r"|UNTRUSTED_JSON_DATA_(?:START|END)",
	re.IGNORECASE | re.DOTALL,
)


def instruction_like_content(value: str) -> bool:
	"""Cheap conservative signal; this is a review guard, not an injection detector."""
	return bool(_INSTRUCTION_LIKE.search(value))


def message_safety_flags(message: Any) -> set[str]:
	"""Keep safety independent from model output and durable with cached bodies."""
	if isinstance(message, dict):
		flags = set(message.get("content_flags") or [])
		body, headers = message.get("body"), message.get("headers") or {}
		extra = "\n".join(str(message.get(key) or "") for key in ("subject", "reason", "correction"))
	else:
		flags = set(getattr(message, "content_flags", []) or [])
		body, headers = getattr(message, "body", ""), getattr(message, "headers", {}) or {}
		extra = ""
	text = str(body or "") + "\n" + "\n".join(str(value) for value in headers.values()) + "\n" + extra
	if instruction_like_content(text):
		flags.add("instruction_like_content")
	return flags


def message_content_hash(message: Message) -> str:
	return canonical_hash({"message_id": message.id, "internalDate": message.internal_ms, "headers": {name.casefold(): normalize_text(message.header(name)).casefold() for name in ("From", "To", "Cc", "Subject", "List-ID")}, "body": normalize_text(message.body or ""), "is_truncated": message.truncated, "content_flags": sorted(message_safety_flags(message))})


def thread_context_hash(messages: Iterable[Message | dict[str, Any]], sent_context_enabled: bool = False) -> str:
	items = []
	for message in messages:
		if isinstance(message, Message):
			mid, millis, labels = message.id, message.internal_ms, message.labels
		else:
			mid = str(message.get("id", message.get("message_id", "")))
			millis = int(message.get("internal_ms", message.get("internalDate", 0)))
			labels = set(message.get("labels", message.get("labelIds", [])))
		items.append((mid, millis, "INBOX" in labels, "SENT" in labels))
	return canonical_hash({"messages": sorted(items), "sent_context_enabled": sent_context_enabled})


def precondition_hash(message: Message, revision: str) -> str:
	return canonical_hash({"message_id": message.id, "labelIds": sorted(message.labels), "context_version": message.context_version, "revision": revision})


def template_key(message: Message) -> str:
	"""Group repeated subjects conservatively; grouping alone never authorizes."""
	subject = re.sub(r"\b\d+\b", "#", normalize_text(message.subject).casefold())
	subject = re.sub(r"^(?:(?:re|fw|fwd):\s*)+", "", subject)
	return canonical_hash([message.sender, message.header("List-ID").strip().casefold(), subject])


def protections(message: Message, config: Config) -> set[str]:
	result: set[str] = set()
	sender = message.sender
	if config.protect_starred and "STARRED" in message.labels:
		result.add("starred")
	if sender in {value.casefold().strip() for value in config.protected_senders}:
		result.add(f"sender:{sender}")
	for value in config.protected_domains:
		protected = value.casefold().strip().lstrip("@")
		if domain_matches(sender, protected):
			result.add(f"domain:{protected}")
	for label_id in message.labels:
		meaning = config.label_meanings.get(label_id, "neutral")
		if label_id in config.protected_label_ids or meaning in {"protected", "attention", "requires_attention", "requires attention"}:
			result.add(f"label:{label_id}")
	return result


def content_excluded(message: Message, config: Config) -> bool:
	sender = message.sender
	return sender in {value.casefold().strip() for value in config.excluded_senders} or any(domain_matches(sender, value) for value in config.excluded_domains) or bool(message.labels.intersection(config.excluded_label_ids))


def eligible(message: Message, window: Window) -> bool:
	return "INBOX" in message.labels and not message.labels.intersection(SYSTEM_EXCLUSIONS) and window.contains(message.internal_ms)


def rule_matches(rule: dict[str, Any], message: Message, assessment: Assessment | None = None) -> bool:
	if not rule.get("enabled", False):
		return False
	conditions = rule.get("conditions", {})
	if not isinstance(conditions, dict) or not conditions:
		return False
	for key, value in conditions.items():
		if key == "sender" and message.sender != str(value).casefold().strip():
			return False
		if key == "domain" and not domain_matches(message.sender, value):
			return False
		if key == "list_id" and message.header("List-ID").strip().casefold() != str(value).strip().casefold():
			return False
		if key == "subject_contains" and str(value).casefold() not in message.subject.casefold():
			return False
		if key == "category" and (assessment is None or assessment.category != value):
			return False
		if key == "template" and template_key(message) != value:
			return False
		if key not in {"sender", "domain", "list_id", "subject_contains", "category", "template"}:
			return False
	return True


def recommend(message: Message, assessment: Assessment | None, config: Config, window: Window, manual_override: dict[str, Any] | None = None, rules: list[dict[str, Any]] | None = None) -> Decision:
	"""Resolve a recommendation; no AI output is itself a mutation instruction.

	The addendum's conservative REVIEW for all outstanding actions is binding,
	even though an earlier section permitted KEEP for certain known tasks.
	"""
	if not eligible(message, window):
		return Decision("skip", "Outside the frozen examination/inbox scope")
	protected = protections(message, config)
	manual = manual_override or {}
	if manual and manual.get("context_version", message.context_version) == message.context_version:
		action = manual.get("action")
		new_protections = protected - set(manual.get("protection_bypass", []))
		if action == "archive" and new_protections:
			return Decision("review", "New or unacknowledged protection invalidates manual archive", evidence_source="manual")
		if action in {"archive", "keep"}:
			return Decision(action, "Explicit current-item manual decision", tuple(manual.get("label_ids", [])), "manual")
		defer_until = manual.get("defer_until", manual.get("defer_until_utc"))
		if action == "defer" and defer_until:
			if as_utc(defer_until) > window.anchor_utc:
				return Decision("keep", "Deferred locally until " + iso_utc(defer_until), evidence_source="manual")
	if protected:
		return Decision("keep", "Protected: " + ", ".join(sorted(protected)), evidence_source="protection")
	flags = message_safety_flags(message)
	if flags:
		return Decision("review", "Content safety review: " + ", ".join(sorted(flags)))
	matched = [rule for rule in (rules or []) if rule_matches(rule, message, assessment)]
	actions = {rule.get("action") for rule in matched if rule.get("action") != "label"}
	independent_labels = tuple(sorted({label for rule in matched if rule.get("action") == "label" for label in rule.get("label_ids", [])}))
	if actions - {"archive", "keep", "review"} or len(actions) > 1:
		return Decision("review", "Conflicting or invalid enabled rules", evidence_source="rule")
	if actions == {"keep"}:
		return Decision("keep", "Enabled rule retains this matched item", independent_labels, "rule")
	if actions == {"review"}:
		return Decision("review", "Enabled rule requests review", independent_labels, "rule")
	if assessment is None:
		if actions == {"archive"} and all(rule.get("source", "explicit") in {"explicit", "approved"} for rule in matched):
			label_ids = tuple(sorted({label for rule in matched for label in rule.get("label_ids", [])}))
			return Decision("archive", "Explicit/approved deterministic rule", label_ids, "rule")
		return Decision("review", "No validated assessment or approved deterministic decision", independent_labels)
	if assessment.message_id != message.id:
		return Decision("review", "Assessment does not match this target")
	if assessment.outstanding_action != "none":
		return Decision("review", "Outstanding attention: " + assessment.outstanding_action, independent_labels)
	if assessment.uncertainty != "low" or assessment.missing_context or message.truncated or message.missing_content:
		return Decision("review", "Incomplete content or uncertain evidence", independent_labels)
	if assessment.keep_in_inbox_score >= config.keep_threshold:
		return Decision("review", "Contradictory no-attention assessment and high inbox score", independent_labels)
	labels = tuple(sorted(set(independent_labels) | (set(assessment.suggested_label_ids) if config.auto_label else set())))
	if actions == {"archive"}:
		return Decision("archive", "Enabled rule and validated no-attention assessment", tuple(sorted(set(labels).union(*(set(rule.get("label_ids", [])) for rule in matched)))), "rule")
	try:
		cutoff = resolve_age_cutoff(config, window.anchor_utc, assessment.category)
	except PolicyConflict:
		return Decision("review", "Conflicting applicable category age overrides")
	if cutoff is not None and message.internal_date < cutoff:
		if assessment.keep_in_inbox_score < config.age_archive_max_score:
			return Decision("archive", "No outstanding attention; older than calendar age cutoff " + iso_utc(cutoff), labels)
		return Decision("review", "Age policy score exceeds archive maximum", independent_labels)
	if assessment.keep_in_inbox_score < config.archive_threshold:
		return Decision("archive", "No outstanding attention; below archive score threshold", labels)
	return Decision("review", "Inbox score lies in the configured review band", independent_labels)


# Readable aliases used by integration adapters and third-party tests.
content_hash = message_content_hash
context_version = thread_context_hash
context_hash = thread_context_hash
precondition_version = precondition_hash
