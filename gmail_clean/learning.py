"""Durable, scoped user feedback, reviewed examples, and approved local rules.

Mailbox observations and application actions are audit evidence, not confirmations
of classifier accuracy. Only explicit reviewed feedback enters the example pool.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TypedDict, cast

from .domain import CATEGORIES, Message, domain_matches, template_key
from .runtime import chunks, placeholders
from .storage import Store, canonical_json, utc_now

_ALLOWED_ACTIONS = {"archive", "keep", "defer", "label", "label_correction", "category_correction", "mistaken_archive", "unnecessary_keep", "review"}
_RULE_ACTIONS = {"archive", "keep", "review", "label"}
_RULE_CONDITIONS = {"sender", "domain", "list_id", "subject_contains", "category", "template"}
_REVIEW_SOURCES = {"manual", "review", "correction"}
_ATTENTION_ACTIONS = {"archive", "keep", "defer", "mistaken_archive", "unnecessary_keep"}


class _PatternStats(TypedDict):
	pattern_key: str
	action: str
	support: int
	counterexamples: int


class Learning:
	def __init__(self, store: Store, enabled: bool = True):
		self.store = store
		self.enabled = enabled
		self._batch_depth = 0
		self._pending_rebuild = False
		self._pending_patterns: set[str] = set()
		self._rebuild_all = False
		self._pending_invalidation: set[tuple[str, str, str, str]] = set()

	@contextlib.contextmanager
	def batch(self):
		"""Keep a bulk review atomic and rebuild derived learning just once."""
		outer = self._batch_depth == 0
		with self.store.transaction():
			self._batch_depth += 1
			try:
				yield self
			except BaseException:
				if outer:
					self._pending_rebuild = False
					self._pending_patterns.clear()
					self._rebuild_all = False
					self._pending_invalidation.clear()
				raise
			finally:
				self._batch_depth -= 1
			if outer:
				if self._pending_rebuild:
					patterns = None if self._rebuild_all else set(self._pending_patterns)
					self._pending_rebuild = False
					self._pending_patterns.clear()
					self._rebuild_all = False
					self._rebuild_stats(patterns)
				self._invalidate_related_batch(self._pending_invalidation)
				self._pending_invalidation.clear()

	def record(
		self, message: Message, action: str, reason: str = "", scope: str = "message",
		group_id: str = "", label_ids: Iterable[str] | None = None,
		negative_label_ids: Iterable[str] | None = None, source: str = "manual", category: str = "",
		defer_until_utc: str | None = None, previous_prediction: dict[str, Any] | None = None,
		policy_version: str = "", model_version: str = "", supersedes: str | None = None,
		correction_of: str | None = None, protection_bypass: Iterable[str] = (),
	) -> str:
		"""Record a reviewed event; normal scope is exactly the displayed message.

		Use one group_id for a bulk command. Repeated rows from that event never
		count as independent support. Label positives do not imply other negatives.
		"""
		if action not in _ALLOWED_ACTIONS:
			raise ValueError("Unsupported feedback action")
		if scope not in {"message", "conversation", "pattern", "sender_condition"}:
			raise ValueError("Feedback scope must be explicit")
		if category and category not in CATEGORIES:
			raise ValueError("Unknown category")
		if defer_until_utc is not None:
			try:
				instant = datetime.fromisoformat(defer_until_utc.replace("Z", "+00:00"))
				if instant.tzinfo is None:
					raise ValueError("Deferral must be timezone aware")
				defer_until_utc = instant.astimezone(timezone.utc).isoformat()
			except (ValueError, TypeError) as exc:
				raise ValueError("Deferral must be an ISO-8601 instant with a timezone") from exc
		if action == "defer" and not defer_until_utc:
			raise ValueError("Choose a deferral end instant")
		positive_labels = sorted(set(label_ids or ()))
		negative_labels = sorted(set(negative_label_ids or ()))
		if set(positive_labels) & set(negative_labels):
			raise ValueError("A label cannot be positive and negative in the same event")
		catalog = {row["id"] for row in self.store.labels() if row["type"] == "user"}
		if set(positive_labels + negative_labels) - catalog:
			raise ValueError("Feedback labels must be existing user labels")
		feedback_id = uuid.uuid4().hex
		event_id = group_id or feedback_id
		now = utc_now()
		eligible_learning = bool(self.enabled and source in _REVIEW_SOURCES)
		payload: dict[str, Any] = {
			"negative_label_ids": negative_labels, "protection_bypass": sorted(set(protection_bypass)),
			"sender": message.sender, "list_id": message.header("List-ID").strip().casefold(),
			"category": category, "pattern_key": template_key(message),
			"defer_until_utc": defer_until_utc,
		}
		with self.store.transaction():
			if supersedes is None and (action in _ATTENTION_ACTIONS or action == "category_correction"):
				previous_rows = self.store.query("SELECT id,action FROM feedback WHERE message_id=? AND context_version=? ORDER BY created_at DESC", (message.id, message.context_version))
				for old in previous_rows:
					if (old["action"] in _ATTENTION_ACTIONS) == (action in _ATTENTION_ACTIONS) and (action in _ATTENTION_ACTIONS or old["action"] == action):
						supersedes = old["id"]
						break
			self.store.execute("""INSERT INTO feedback
				(id,message_id,action,source,reason,scope,group_id,context_version,previous_prediction,
				policy_version,model_version,label_ids_json,supersedes,correction_of,include_learning,created_at,payload_json)
				VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
				feedback_id, message.id, action, source, reason[:1000], scope, event_id, message.context_version,
				canonical_json(previous_prediction or {}), policy_version, model_version, canonical_json(positive_labels),
				supersedes, correction_of, int(eligible_learning), now, canonical_json(payload),
			))
			if source in _REVIEW_SOURCES and action in _ATTENTION_ACTIONS:
				override_action = {"mistaken_archive": "keep", "unnecessary_keep": "archive"}.get(action, action)
				override_payload = {**payload, "label_ids": positive_labels, "feedback_id": feedback_id}
				self.store.execute("""INSERT INTO manual_overrides(message_id,context_version,action,defer_until_utc,feedback_id,updated_at,payload_json)
					VALUES(?,?,?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET context_version=excluded.context_version,
					action=excluded.action,defer_until_utc=excluded.defer_until_utc,feedback_id=excluded.feedback_id,
					updated_at=excluded.updated_at,payload_json=excluded.payload_json""", (
					message.id, message.context_version, override_action, defer_until_utc, feedback_id, now,
					canonical_json(override_payload),
				))
				self.store.execute("UPDATE messages SET defer_until_utc=? WHERE id=?", (defer_until_utc if action == "defer" else None, message.id))
			if eligible_learning:
				# A newer explicit correction replaces support from an earlier reviewed
				# decision while retaining the original immutable feedback audit row.
				if supersedes:
					self.store.execute("DELETE FROM examples WHERE feedback_id=?", (supersedes,))
				self._add_example(feedback_id, message, action, reason, category, positive_labels, event_id, now, payload)
				self._rebuild_stats({payload["pattern_key"]})
				self._invalidate_related(message.sender, category, payload["list_id"], message.id)
		return feedback_id

	def _add_example(self, feedback_id: str, message: Message, action: str, reason: str, category: str,
		label_ids: list[str], group_id: str, created_at: str, payload: dict[str, Any]) -> None:
		self.store.execute("""INSERT INTO examples
			(id,feedback_id,message_id,sender,list_id,category,pattern_key,action,reason,label_ids_json,
			body,subject,context_version,group_id,weight,created_at,payload_json)
			VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
			uuid.uuid4().hex, feedback_id, message.id, message.sender, payload["list_id"], category,
			payload["pattern_key"], action, reason[:1000], canonical_json(label_ids), (message.body or "")[:4000],
			message.subject[:500], message.context_version, group_id,
			3.0 if action in {"mistaken_archive", "unnecessary_keep", "category_correction", "label_correction"} else 1.0,
			created_at, canonical_json({**payload, "labels": sorted(message.labels), "truncated": message.truncated}),
		))

	def manual_override(self, message: Message) -> dict[str, Any] | None:
		row = self.store.one("SELECT * FROM manual_overrides WHERE message_id=?", (message.id,))
		if not row or row["context_version"] != message.context_version:
			return None
		return {**json.loads(row["payload_json"]), **row}

	def examples_for(
		self, message: Message, limit: int = 6, excluded_senders: Iterable[str] = (),
		excluded_domains: Iterable[str] = (), excluded_categories: Iterable[str] = (),
		excluded_labels: Iterable[str] = (), excluded_label_ids: Iterable[str] | None = None,
	) -> list[dict[str, Any]]:
		"""Select bounded representative reviewed examples, including counterexamples.

		Sharing exclusions are applied to both targets and historical examples.
		No examples are supplied while learning is disabled.
		"""
		if not self.enabled or limit <= 0:
			return []
		excluded_sender_set = {value.casefold().strip() for value in excluded_senders}
		excluded_domain_set = {value.casefold().strip().lstrip("@") for value in excluded_domains}
		excluded_category_set = set(excluded_categories)
		excluded_label_set = set(excluded_labels) | set(excluded_label_ids or ())
		current = self.store.one("SELECT category FROM messages WHERE id=?", (message.id,))
		category = current["category"] if current else ""
		if _excluded(message.sender, category, message.labels, excluded_sender_set, excluded_domain_set, excluded_category_set, excluded_label_set):
			return []
		list_id = message.header("List-ID").strip().casefold()
		pattern = template_key(message)
		rows = self.store.query("""SELECT e.*,m.labels_json AS current_labels_json,m.category AS current_category
			FROM examples e LEFT JOIN messages m ON m.id=e.message_id WHERE e.message_id!=? AND
			(e.sender=? OR e.pattern_key=? OR (e.list_id!='' AND e.list_id=?) OR (e.category!='' AND e.category=?))
			ORDER BY e.weight DESC,e.created_at DESC LIMIT 300""", (message.id, message.sender, pattern, list_id, category))
		candidates = []
		for row in rows:
			payload = json.loads(row["payload_json"])
			sharing_labels = set(payload.get("labels", []))
			sharing_categories = {row["category"]}
			if row["current_labels_json"] is not None:
				sharing_labels.update(json.loads(row["current_labels_json"]))
			if row["current_category"] is not None:
				sharing_categories.add(row["current_category"])
			if sharing_categories & excluded_category_set:
				continue
			if _excluded(row["sender"], row["category"], sharing_labels, excluded_sender_set, excluded_domain_set, excluded_category_set, excluded_label_set):
				continue
			score = (8 if row["pattern_key"] == pattern else 0) + (4 if list_id and row["list_id"] == list_id else 0) + (2 if row["sender"] == message.sender else 0) + (1 if category and row["category"] == category else 0) + float(row["weight"])
			candidates.append((score, row))
		candidates.sort(key=lambda item: (item[0], item[1]["created_at"]), reverse=True)
		# One event/template/action representative prevents bulk rows dominating.
		seen = set()
		selected = []
		for _, row in candidates:
			key = (row["group_id"], row["pattern_key"], row["action"])
			if key in seen:
				continue
			seen.add(key)
			payload = json.loads(row["payload_json"])
			selected.append({
				"id": row["id"], "message_id": row["message_id"], "sender": row["sender"],
				"subject": row["subject"], "body": row["body"], "category": row["category"],
				"action": row["action"], "reason": row["reason"], "label_ids": json.loads(row["label_ids_json"]),
				"negative_label_ids": payload.get("negative_label_ids", []),
				"context_version": row["context_version"], "created_at": row["created_at"],
				"source": "explicit reviewed example", "weight": row["weight"],
			})
			if len(selected) >= min(limit, 20):
				break
		return selected

	def examples_version(self, examples: list[dict[str, Any]]) -> str:
		return hashlib.sha256(canonical_json(examples).encode()).hexdigest()

	def _rebuild_stats(self, patterns: Iterable[str] | None = None) -> None:
		"""Rebuild affected template families; explicit full reset may rebuild all.

		Independent review-event IDs remain the support unit. Ordinary feedback
		must not read/rewrite every unrelated pattern or change its timestamps.
		"""
		selected = set(patterns) if patterns is not None else None
		if self._batch_depth:
			self._pending_rebuild = True
			if selected is None:
				self._rebuild_all = True
			else:
				self._pending_patterns.update(selected)
			return
		if selected is not None and not selected:
			return
		rows = []
		rules = []
		if selected is None:
			self.store.execute("DELETE FROM pattern_stats")
			rows = self.store.query("SELECT pattern_key,action,group_id,sender,list_id,subject,created_at,id FROM examples ORDER BY created_at DESC,id DESC")
			rules = self.store.query("SELECT * FROM rules WHERE json_extract(payload_json,'$.pattern_key') IS NOT NULL")
		else:
			# Keep bulk resets within SQLite's parameter limit without N+1 queries.
			ordered = sorted(selected)
			for chunk in chunks(ordered):
				marks = placeholders(chunk)
				self.store.execute(f"DELETE FROM pattern_stats WHERE pattern_key IN ({marks})", chunk)  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
				rows.extend(self.store.query(f"SELECT pattern_key,action,group_id,sender,list_id,subject,created_at,id FROM examples WHERE pattern_key IN ({marks}) ORDER BY created_at DESC,id DESC", chunk))  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
				rules.extend(self.store.query(f"SELECT * FROM rules WHERE json_extract(payload_json,'$.pattern_key') IN ({marks})", chunk))  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
		groups: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
		samples: dict[str, dict[str, Any]] = {}
		for row in rows:
			action = {"mistaken_archive": "keep", "unnecessary_keep": "archive"}.get(row["action"], row["action"])
			groups[row["pattern_key"]][action].add(row["group_id"])
			samples.setdefault(row["pattern_key"], row)
		statistics: dict[tuple[str, str], _PatternStats] = {}
		for pattern, actions in groups.items():
			for action, events in actions.items():
				conflicts = set().union(*(items for other, items in actions.items() if other in {"archive", "keep"} and action in {"archive", "keep"} and other != action))
				self.store.execute("INSERT INTO pattern_stats(pattern_key,action,support,counterexamples,updated_at) VALUES(?,?,?,?,?)", (pattern, action, len(events), len(conflicts), utc_now()))
				statistics[(pattern, action)] = {"pattern_key": pattern, "action": action, "support": len(events), "counterexamples": len(conflicts)}
		# Suggestions are derived and removed when source evidence is forgotten.
		existing = set()
		for rule in rules:
			payload = json.loads(rule["payload_json"])
			pattern = payload.get("pattern_key")
			key = (pattern, rule["action"])
			if rule["source"] != "suggested":
				existing.add(key)
				continue
			stats = statistics.get(key)
			unsafe_archive = rule["action"] == "archive" and not _safe_archive_pattern(samples.get(pattern))
			if not stats or stats["support"] < 3 or unsafe_archive:
				self.store.execute("DELETE FROM rules WHERE id=?", (rule["id"],))
				self.store.set_checkpoint("rules_revision", uuid.uuid4().hex)
			else:
				self.store.execute("UPDATE rules SET support=?,counterexamples=?,updated_at=? WHERE id=?", (stats["support"], stats["counterexamples"], utc_now(), rule["id"]))
				existing.add(key)
		for key, stats in statistics.items():
			if stats["action"] not in {"archive", "keep"} or stats["support"] < 3 or key in existing:
				continue
			sample = samples.get(stats["pattern_key"])
			if not sample or (stats["action"] == "archive" and not _safe_archive_pattern(sample)):
				continue
			conditions = {"sender": sample["sender"], "template": stats["pattern_key"]}
			self.save_rule({"name": f"Reviewed pattern: {sample['subject'][:60]}", "conditions": conditions,
				"action": stats["action"], "enabled": False, "source": "suggested", "support": stats["support"],
				"counterexamples": stats["counterexamples"], "payload": {"pattern_key": stats["pattern_key"],
				"evidence_basis": "Independent explicit review events; never automatic successes"}}, invalidate=False)

	def rules(self, include_disabled: bool = True) -> list[dict[str, Any]]:
		rows = self.store.query("SELECT * FROM rules" + ("" if include_disabled else " WHERE enabled=1") + " ORDER BY enabled DESC,updated_at DESC")  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
		return [self._decode_rule(row) for row in rows]

	def _decode_rule(self, row: dict[str, Any]) -> dict[str, Any]:
		return {**row, "conditions": json.loads(row["conditions_json"]), "label_ids": json.loads(row["label_ids_json"]),
			"enabled": bool(row["enabled"]), "payload": json.loads(row["payload_json"])}

	def save_rule(self, rule: dict[str, Any], invalidate: bool = True) -> str:
		conditions = rule.get("conditions")
		if conditions is None and "conditions_json" in rule:
			conditions = json.loads(rule["conditions_json"])
		if not isinstance(conditions, dict) or not conditions or set(conditions) - _RULE_CONDITIONS:
			raise ValueError("Use supported, nonempty rule conditions")
		if any(not isinstance(value, str) or not value.strip() for value in conditions.values()):
			raise ValueError("Rule conditions must be nonempty strings")
		action = rule.get("action", "review")
		if action not in _RULE_ACTIONS:
			raise ValueError("Rules can archive, keep, review, or add existing labels")
		if "category" in conditions and conditions["category"] not in CATEGORIES:
			raise ValueError("Unknown rule category")
		if action == "archive" and not ("template" in conditions or "list_id" in conditions or ("sender" in conditions and len(conditions) >= 2)):
			raise ValueError("Archive rules need a recurring template/list or sender plus a condition")
		labels = sorted(set(rule.get("label_ids", [])))
		catalog = {row["id"] for row in self.store.labels() if row["type"] == "user"}
		if set(labels) - catalog:
			raise ValueError("Rules may only add existing user labels")
		rule_id = str(rule.get("id") or uuid.uuid4().hex)
		old = self.store.one("SELECT * FROM rules WHERE id=?", (rule_id,))
		source = rule.get("source", old["source"] if old else "explicit")
		if source not in {"explicit", "suggested", "approved"}:
			raise ValueError("Unknown rule source")
		enabled = bool(rule.get("enabled", old["enabled"] if old else False))
		if source == "suggested" and enabled:
			# Enabling a learned suggestion is an explicit reviewed rule decision.
			source = "approved"
		now = utc_now()
		payload = rule.get("payload", json.loads(old["payload_json"]) if old else {})
		with self.store.transaction():
			self.store.execute("""INSERT INTO rules
				(id,name,conditions_json,action,label_ids_json,enabled,source,revision,support,counterexamples,created_at,updated_at,payload_json)
				VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
				conditions_json=excluded.conditions_json,action=excluded.action,label_ids_json=excluded.label_ids_json,
				enabled=excluded.enabled,source=excluded.source,revision=excluded.revision,support=excluded.support,
				counterexamples=excluded.counterexamples,updated_at=excluded.updated_at,payload_json=excluded.payload_json""", (
				rule_id, str(rule.get("name", "Explicit rule"))[:200], canonical_json(conditions), action,
				canonical_json(labels), int(enabled), source, int(old["revision"]) + 1 if old else 1,
				int(rule.get("support", old["support"] if old else 0)), int(rule.get("counterexamples", old["counterexamples"] if old else 0)),
				old["created_at"] if old else now, now, canonical_json(payload),
			))
			if invalidate:
				if old and json.loads(old["conditions_json"]) != conditions:
					self._invalidate_rule_matches(json.loads(old["conditions_json"]))
				self._invalidate_rule_matches(conditions)
			self.store.set_checkpoint("rules_revision", uuid.uuid4().hex)
		return rule_id

	def set_rule_enabled(self, rule_id: str, enabled: bool) -> None:
		row = self.store.one("SELECT * FROM rules WHERE id=?", (rule_id,))
		if not row:
			raise KeyError("Rule does not exist")
		rule = self._decode_rule(row)
		rule["enabled"] = bool(enabled)
		self.save_rule(rule)

	def delete_rule(self, rule_id: str) -> None:
		row = self.store.one("SELECT conditions_json FROM rules WHERE id=?", (rule_id,))
		if row:
			with self.store.transaction():
				self.store.execute("DELETE FROM rules WHERE id=?", (rule_id,))
				self.store.set_checkpoint("rules_revision", uuid.uuid4().hex)
				self._invalidate_rule_matches(json.loads(row["conditions_json"]))

	def pattern_suggestions(self) -> list[dict[str, Any]]:
		return [rule for rule in self.rules() if rule["source"] == "suggested"]

	def rule_preview(self, rule_or_id: dict[str, Any] | str, limit: int = 10) -> dict[str, Any]:
		"""Inspect explicit matching IDs and counts without broadening action scope."""
		if isinstance(rule_or_id, str):
			row = self.store.one("SELECT * FROM rules WHERE id=?", (rule_or_id,))
			if not row:
				raise KeyError("Rule does not exist")
			rule = self._decode_rule(row)
		else:
			rule = dict(rule_or_id)
		conditions = rule.get("conditions", {})
		clauses, params = _rule_sql(conditions)
		where = "in_inbox=1 AND " + " AND ".join(clauses or ["1"])
		sample_limit = max(0, min(limit, 100))
		samples: list[dict[str, Any]] = []
		if "template" in conditions:
			count, earliest, latest = 0, None, None
			# Stream metadata only; templates never require loading each cached body.
			for raw in self.store.execute("SELECT id,internal_ms,subject,sender,headers_json FROM messages WHERE " + where + " ORDER BY internal_ms,id", params):  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
				row = dict(raw)
				if _row_template(row) != conditions["template"]:
					continue
				count += 1
				earliest = row["internal_ms"] if earliest is None else earliest
				latest = row["internal_ms"]
				if len(samples) < sample_limit:
					samples.append({key: row[key] for key in ("id", "internal_ms", "subject", "sender")})
		else:
			bounds = cast(dict[str, Any], self.store.one("SELECT COUNT(*) n,MIN(internal_ms) earliest,MAX(internal_ms) latest FROM messages WHERE " + where, params))  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
			count, earliest, latest = bounds["n"], bounds["earliest"], bounds["latest"]
			if sample_limit:
				samples = self.store.query("SELECT id,internal_ms,subject,sender FROM messages WHERE " + where + " ORDER BY internal_ms,id LIMIT ?", [*params, sample_limit])  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
		return {"count": count, "sample_ids": [row["id"] for row in samples],
			"samples": samples,
			"earliest_internal_ms": earliest,
			"latest_internal_ms": latest,
			"support": rule.get("support", 0), "counterexamples": rule.get("counterexamples", 0),
			"conditions": conditions, "revision": rule.get("revision", 1),
			"scope_note": "Current local inbox matches; job eligibility and protections are checked separately"}

	def export_rules(self, path: str | Path) -> Path:
		target = Path(path)
		target.parent.mkdir(parents=True, exist_ok=True)
		data = [{key: rule[key] for key in ("id", "name", "conditions", "action", "label_ids", "enabled", "source", "revision", "support", "counterexamples", "payload")} for rule in self.rules()]
		target.write_text(json.dumps({"schema_version": "1.0", "rules": data}, indent=2, ensure_ascii=False), encoding="utf-8")
		return target

	def import_rules(self, path: str | Path) -> int:
		target = Path(path)
		if target.stat().st_size > 2_000_000:
			raise ValueError("Rule import exceeds the two-megabyte limit")
		data = json.loads(target.read_text(encoding="utf-8"))
		if not isinstance(data, dict) or data.get("schema_version") != "1.0" or not isinstance(data.get("rules"), list):
			raise ValueError("Invalid rule export schema")
		if len(data["rules"]) > 2000:
			raise ValueError("Rule import contains too many rules")
		with self.store.transaction():
			for rule in data["rules"]:
				if not isinstance(rule, dict):
					raise ValueError("Each rule must be an object")
				# Imported rules start disabled; enabling is a visible user decision.
				self.save_rule({**rule, "enabled": False, "source": "explicit"})
		return len(data["rules"])

	def forget(self, example_id: str) -> None:
		row = self.store.one("SELECT * FROM examples WHERE id=?", (example_id,))
		if not row:
			return
		with self.store.transaction():
			self.store.execute("DELETE FROM examples WHERE id=?", (example_id,))
			self.store.execute("UPDATE feedback SET include_learning=0 WHERE id=?", (row["feedback_id"],))
			self._rebuild_stats({row["pattern_key"]})
			self._invalidate_related(row["sender"], row["category"], row["list_id"], row["message_id"])

	def reset(self, sender: str | None = None, category: str | None = None, pattern_key: str | None = None) -> int:
		clauses, params = [], []
		for column, value in (("sender", sender.casefold().strip() if sender else None), ("category", category), ("pattern_key", pattern_key)):
			if value is not None:
				clauses.append(f"{column}=?")
				params.append(value)
		rows = self.store.query("SELECT * FROM examples" + (" WHERE " + " AND ".join(clauses) if clauses else ""), params)  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
		with self.batch():
			for row in rows:
				self.store.execute("DELETE FROM examples WHERE id=?", (row["id"],))
				self.store.execute("UPDATE feedback SET include_learning=0 WHERE id=?", (row["feedback_id"],))
				self._invalidate_related(row["sender"], row["category"], row["list_id"], row["message_id"])
			self._rebuild_stats({row["pattern_key"] for row in rows} if clauses else None)
		return len(rows)

	def include_feedback(self, feedback_id: str) -> None:
		"""Explicit opt-in for an event recorded while learning was disabled."""
		if not self.enabled:
			raise ValueError("Enable learning before explicitly including feedback")
		row = self.store.one("SELECT * FROM feedback WHERE id=?", (feedback_id,))
		if not row or row["source"] not in _REVIEW_SOURCES:
			raise ValueError("Only explicit reviewed feedback may be included")
		if self.store.one("SELECT id FROM examples WHERE feedback_id=?", (feedback_id,)):
			return
		message = self.store.get_message(row["message_id"])
		if not message or message.context_version != row["context_version"]:
			raise ValueError("The original reviewed context is no longer available")
		payload = json.loads(row["payload_json"])
		with self.store.transaction():
			self._add_example(feedback_id, message, row["action"], row["reason"], payload.get("category", ""), json.loads(row["label_ids_json"]), row["group_id"], row["created_at"], payload)
			self.store.execute("UPDATE feedback SET include_learning=1 WHERE id=?", (feedback_id,))
			self._rebuild_stats({payload.get("pattern_key") or template_key(message)})
			self._invalidate_related(message.sender, payload.get("category", ""), payload.get("list_id", ""), message.id)

	def quality_metrics(self) -> dict[str, Any]:
		rows = self.store.query("SELECT action,COUNT(*) AS count,COUNT(DISTINCT group_id) AS independent_events FROM feedback WHERE source IN ('manual','review','correction') GROUP BY action")
		return {
			"basis": "Explicit reviewed events only; no estimate of unreviewed automatic accuracy",
			"counts": {row["action"]: row["count"] for row in rows},
			"independent_events": {row["action"]: row["independent_events"] for row in rows},
			"examples": cast(dict[str, Any], self.store.one("SELECT COUNT(*) AS n FROM examples"))["n"],
			"enabled_rules": cast(dict[str, Any], self.store.one("SELECT COUNT(*) AS n FROM rules WHERE enabled=1"))["n"],
			"reviewed_by_type": self.store.query("SELECT category,action,COUNT(DISTINCT group_id) AS events FROM examples GROUP BY category,action ORDER BY category,action"),
		}

	def _invalidate_related(self, sender: str, category: str, list_id: str, message_id: str) -> None:
		if self._batch_depth:
			self._pending_invalidation.add((sender, category, list_id, message_id))
			return
		conditions = ["id=?"]
		params: list[Any] = [message_id]
		if sender:
			conditions.append("sender=?")
			params.append(sender)
		if category and not sender and not list_id:
			conditions.append("category=?")
			params.append(category)
		if list_id:
			conditions.append("list_id=?")
			params.append(list_id.strip().casefold())
		self._invalidate_selected("SELECT id FROM messages WHERE " + " OR ".join(conditions), params, "Relevant reviewed examples changed")  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound

	def _invalidate_related_batch(self, related: Iterable[tuple[str, str, str, str]]) -> None:
		# A bulk review of one list should not update the same list's predictions
		# once per selected email. Union scopes, preserving every explicit ID.
		groups: dict[str, set[str]] = {"id": set(), "sender": set(), "list_id": set(), "category": set()}
		for sender, category, list_id, message_id in related:
			groups["id"].add(message_id)
			if sender:
				groups["sender"].add(sender)
			if list_id:
				groups["list_id"].add(list_id.strip().casefold())
			if category and not sender and not list_id:
				groups["category"].add(category)
		for column, values in groups.items():
			ordered = sorted(values)
			for chunk in chunks(ordered):
				selection = "SELECT id FROM messages WHERE " + column + " IN (" + placeholders(chunk) + ")"  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
				self._invalidate_selected(selection, chunk, "Relevant reviewed examples changed")

	def _invalidate_rule_matches(self, conditions: dict[str, str]) -> None:
		# Narrow SQL preselection avoids invalidating the entire cache after an
		# edit to a single sender/type rule. Unknown assessments stay pending.
		clauses, params = _rule_sql(conditions)
		where = " WHERE " + " AND ".join(clauses) if clauses else ""
		if "template" not in conditions:
			self._invalidate_selected("SELECT id FROM messages" + where, params, "Rule semantics changed")  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
			return
		ids = []
		for raw in self.store.execute("SELECT id,headers_json FROM messages" + where, params):  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
			row = dict(raw)
			if _row_template(row) == conditions["template"]:
				ids.append(row["id"])
			if len(ids) == 400:
				self._invalidate_selected("SELECT id FROM messages WHERE id IN (" + placeholders(ids) + ")", ids, "Rule semantics changed")  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
				ids.clear()
		if ids:
			self._invalidate_selected("SELECT id FROM messages WHERE id IN (" + placeholders(ids) + ")", ids, "Rule semantics changed")  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound

	def _invalidate_selected(self, selection: str, params: list[Any], reason: str) -> None:
		self.store.execute("UPDATE predictions SET valid=0 WHERE message_id IN (" + selection + ")", params)  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound
		# Reviewed instructions and completed receipts survive learning/rule edits;
		# only unexecuted automatic proposals need fresh interpretation/preflight.
		self.store.execute("UPDATE actions SET state='stale',reason=?,updated_at=? WHERE approval='automatic' AND state IN ('queued','proposed','retry') AND message_id IN (" + selection + ")", [reason, utc_now(), *params])  # noqa: S608 -- internal predicate builders and fixed column allowlists; values stay bound


def _excluded(sender: str, category: str, labels: Iterable[str], senders: set[str], domains: set[str], categories: set[str], label_ids: set[str]) -> bool:
	return sender.casefold() in senders or any(domain_matches(sender, target) for target in domains) or category in categories or bool(set(labels) & label_ids)


def _safe_archive_pattern(sample: dict[str, Any] | None) -> bool:
	if not sample:
		return False
	if sample["list_id"].strip():
		return True
	cleaned = re.sub(r"^(?:(?:re|fw|fwd):\s*)+", "", sample["subject"].strip(), flags=re.IGNORECASE)
	return sum(character.isalpha() for character in cleaned if not character.isdigit()) >= 4


def _row_template(row: dict[str, Any]) -> str:
	return template_key(Message(id=row["id"], thread_id="", internal_ms=row.get("internal_ms", 0), headers=json.loads(row["headers_json"] or "{}")))


def _rule_sql(conditions: dict[str, str]) -> tuple[list[str], list[str]]:
	"""SQL preselection shares exact/subdomain and whitespace semantics with policy."""
	clauses, params = [], []
	for key, value in conditions.items():
		if key == "sender":
			clauses.append("sender=?")
			params.append(value.casefold().strip())
		elif key == "domain":
			domain = value.casefold().strip().lstrip("@")
			if not domain:
				clauses.append("0")
				continue
			escaped = domain.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
			clauses.append("(substr(sender,instr(sender,'@')+1)=? OR substr(sender,instr(sender,'@')+1) LIKE ? ESCAPE '\\')")
			params.extend((domain, "%." + escaped))
		elif key == "category":
			clauses.append("category=?")
			params.append(value)
		elif key == "subject_contains":
			clauses.append("instr(lower(subject),?)>0")
			params.append(value.casefold())
		elif key == "list_id":
			clauses.append("list_id=?")
			params.append(value.casefold().strip())
		elif key != "template":
			raise ValueError("Unsupported rule condition")
	return clauses, params
