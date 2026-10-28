from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from gmail_clean.domain import Message, template_key
from gmail_clean.learning import Learning
from gmail_clean.storage import Store


@pytest.fixture
def setup(tmp_path):
	store = Store(tmp_path / "learning.sqlite3", "me@example.invalid")
	store.replace_labels([{"id": "L1", "name": "Records", "type": "user"}, {"id": "L2", "name": "Bills", "type": "user"}])
	learning = Learning(store)
	yield store, learning
	store.close()


def mail(mid="m1", sender="news@example.invalid", subject="Monthly issue 42", context="v1"):
	return Message(mid, "t-" + mid, 1_700_000_000_000, {"INBOX", "L1"}, {"From": sender, "Subject": subject, "List-ID": "newsletter.example.invalid"}, body="A useful digest of this month's news", context_version=context, account_id="me@example.invalid")


def add(store, message):
	store.upsert_message(message)
	store.execute("UPDATE messages SET category='newsletter' WHERE id=?", (message.id,))
	return message


def test_archive_is_attention_feedback_and_labels_only_positive(setup):
	store, learning = setup
	message = add(store, mail())
	archive = learning.record(message, "archive", reason="completed", category="newsletter")
	learning.record(message, "label", label_ids=["L2"], category="newsletter")
	examples = store.query("SELECT action,label_ids_json,payload_json FROM examples ORDER BY created_at")
	assert len(examples) == 2
	assert examples[0]["action"] == "archive"
	assert '"negative_label_ids":[]' in examples[1]["payload_json"]
	assert learning.manual_override(message)["action"] == "archive"
	assert learning.manual_override(message)["feedback_id"] == archive
	assert learning.manual_override(replace(message, context_version="new-reply")) is None


def test_bulk_does_not_become_independent_support_or_false_accuracy(setup):
	store, learning = setup
	for index in range(12):
		learning.record(add(store, mail(f"m{index}", subject=f"Monthly issue {index}")), "archive", group_id="one-bulk-review", category="newsletter")
	stats = store.one("SELECT support,counterexamples FROM pattern_stats WHERE action='archive'")
	assert stats == {"support": 1, "counterexamples": 0}
	assert learning.pattern_suggestions() == []
	assert learning.quality_metrics()["independent_events"]["archive"] == 1
	learning.record(add(store, mail("auto")), "archive", source="automatic", category="newsletter")
	assert store.one("SELECT COUNT(*) AS n FROM examples")["n"] == 12
	assert learning.quality_metrics()["counts"]["archive"] == 12


def test_inferred_rule_needs_enable_and_has_counterexamples(setup):
	store, learning = setup
	for index in range(3):
		learning.record(add(store, mail(f"m{index}", subject=f"Monthly issue {index}")), "archive", category="newsletter")
	suggestion = learning.pattern_suggestions()[0]
	assert suggestion["support"] == 3 and not suggestion["enabled"]
	assert "template" in suggestion["conditions"]
	learning.record(add(store, mail("exception", subject="Monthly issue 999")), "keep", reason="reply needed", category="newsletter")
	assert learning.pattern_suggestions()[0]["counterexamples"] == 1
	learning.set_rule_enabled(suggestion["id"], True)
	rule = learning.rules(include_disabled=False)[0]
	assert rule["enabled"] and rule["source"] == "approved"


def test_recent_correction_replaces_old_support_and_reaches_prompts(setup):
	store, learning = setup
	message = add(store, mail())
	old = learning.record(message, "archive", category="newsletter")
	learning.record(message, "mistaken_archive", reason="still awaiting my reply", category="newsletter", correction_of=old)
	assert store.one("SELECT COUNT(*) AS n FROM examples")["n"] == 1
	assert learning.manual_override(message)["action"] == "keep"
	examples = learning.examples_for(add(store, mail("target")))
	assert len(examples) == 1
	assert examples[0]["action"] == "mistaken_archive" and examples[0]["weight"] == 3
	assert examples[0]["reason"] == "still awaiting my reply"


def test_learning_disabled_keeps_manual_overrides_and_requires_opt_in(setup):
	store, learning = setup
	message = add(store, mail())
	learning.enabled = False
	feedback_id = learning.record(message, "keep", reason="task pending", category="newsletter")
	assert learning.manual_override(message)["action"] == "keep"
	assert not store.query("SELECT * FROM examples")
	assert not learning.examples_for(mail("target"))
	learning.enabled = True
	assert not store.query("SELECT * FROM examples")
	learning.include_feedback(feedback_id)
	assert store.one("SELECT action FROM examples")["action"] == "keep"


def test_sharing_exclusions_apply_to_historical_examples_and_target(setup):
	store, learning = setup
	message = add(store, mail())
	learning.record(message, "keep", category="newsletter")
	target = add(store, mail("target"))
	assert learning.examples_for(target)
	assert not learning.examples_for(target, excluded_domains=["example.invalid"])
	assert not learning.examples_for(target, excluded_labels=["L1"])
	assert not learning.examples_for(target, excluded_categories=["newsletter"])
	assert not learning.examples_for(target, excluded_senders=[message.sender])


def test_defer_is_local_persistent_and_context_scoped(setup):
	store, learning = setup
	message = add(store, mail())
	until = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
	learning.record(message, "defer", defer_until_utc=until)
	assert learning.manual_override(message)["defer_until_utc"] == until
	assert store.one("SELECT defer_until_utc FROM messages WHERE id=?", (message.id,))["defer_until_utc"] == until
	assert message.labels == {"INBOX", "L1"}
	with pytest.raises(ValueError, match="timezone"):
		learning.record(message, "defer", defer_until_utc="2026-12-01T12:00:00")


def test_forget_rebuilds_derived_state_invalidates_relevant_only_preserves_undo(setup):
	store, learning = setup
	for index in range(3):
		learning.record(add(store, mail(f"m{index}")), "archive", category="newsletter")
	assert learning.pattern_suggestions()
	store.execute("INSERT INTO actions(id,message_id,state) VALUES('audit','m0','completed')")
	store.execute("INSERT INTO predictions(id,message_id,valid) VALUES('related','m0',1)")
	other = add(store, mail("unrelated", sender="other@different.invalid"))
	store.execute("UPDATE messages SET category='receipt',headers_json='{}' WHERE id=?", (other.id,))
	store.execute("INSERT INTO predictions(id,message_id,valid) VALUES('unrelated','unrelated',1)")
	example = store.one("SELECT id FROM examples LIMIT 1")
	learning.forget(example["id"])
	assert not learning.pattern_suggestions()
	assert store.one("SELECT support FROM pattern_stats WHERE action='archive'")["support"] == 2
	assert store.one("SELECT valid FROM predictions WHERE id='related'")["valid"] == 0
	assert store.one("SELECT valid FROM predictions WHERE id='unrelated'")["valid"] == 1
	assert store.one("SELECT state FROM actions WHERE id='audit'")["state"] == "completed"
	assert learning.manual_override(store.get_message("m0"))


def test_reset_and_rule_import_export_are_real_operations(setup, tmp_path):
	store, learning = setup
	message = add(store, mail())
	learning.record(message, "label_correction", negative_label_ids=["L1"], label_ids=["L2"], category="newsletter")
	rule_id = learning.save_rule({"name": "Monthly digest", "conditions": {"sender": message.sender, "template": template_key(message)}, "action": "archive", "enabled": True})
	path = learning.export_rules(tmp_path / "rules.json")
	learning.delete_rule(rule_id)
	assert not learning.rules()
	assert learning.import_rules(path) == 1
	assert not learning.rules()[0]["enabled"]
	assert learning.reset(sender=message.sender) == 1
	assert not store.query("SELECT * FROM examples")
	assert not store.query("SELECT * FROM pattern_stats")
	assert learning.rules()  # Explicit user rules are managed separately.


def test_rules_reject_broad_or_unauthorized_operations_and_import_rolls_back(setup, tmp_path):
	store, learning = setup
	with pytest.raises(ValueError, match="recurring"):
		learning.save_rule({"conditions": {"sender": "a@example.invalid"}, "action": "archive"})
	with pytest.raises(ValueError, match="supported"):
		learning.save_rule({"conditions": {"execute": "anything"}, "action": "archive"})
	with pytest.raises(ValueError, match="existing user labels"):
		learning.save_rule({"conditions": {"list_id": "known-list"}, "action": "label", "label_ids": ["invented"]})
	path = tmp_path / "invalid.json"
	path.write_text('{"schema_version":"1.0","rules":[{"conditions":{"sender":"x@example.invalid"},"action":"keep"},{"conditions":{},"action":"delete"}]}')
	with pytest.raises(ValueError):
		learning.import_rules(path)
	assert not learning.rules()


def test_positive_label_events_do_not_replace_each_other(setup):
	store, learning = setup
	message = add(store, mail())
	learning.record(message, "label", label_ids=["L1"])
	learning.record(message, "label", label_ids=["L2"])
	rows = store.query("SELECT label_ids_json FROM examples")
	assert {row["label_ids_json"] for row in rows} == {'["L1"]', '["L2"]'}


def test_batch_is_atomic_and_current_privacy_labels_filter_history(setup):
	store, learning = setup
	with learning.batch():
		for index in range(4):
			learning.record(add(store, mail(f"batch-{index}")), "archive", category="newsletter")
	assert learning.pattern_suggestions()[0]["support"] == 4
	target = add(store, mail("target"))
	assert learning.examples_for(target)
	for row in store.query("SELECT message_id FROM examples"):
		store.execute("UPDATE messages SET labels_json='[\"INBOX\",\"L2\"]' WHERE id=?", (row["message_id"],))
	assert not learning.examples_for(target, excluded_labels=["L2"])
	before = store.one("SELECT COUNT(*) AS n FROM feedback")["n"]
	with pytest.raises(RuntimeError):
		with learning.batch():
			learning.record(target, "keep")
			raise RuntimeError("cancel review batch")
	assert store.one("SELECT COUNT(*) AS n FROM feedback")["n"] == before


def test_rule_preview_exposes_sample_ids_and_effective_local_count(setup):
	store, learning = setup
	for index in range(5):
		add(store, mail(f"m{index}"))
	rule_id = learning.save_rule({"conditions": {"sender": "news@example.invalid", "template": template_key(mail())}, "action": "archive"})
	preview = learning.rule_preview(rule_id, limit=2)
	assert preview["count"] == 5
	assert len(preview["sample_ids"]) == 2
	assert preview["revision"] == 1


def test_reviewing_one_sender_preserves_other_senders_in_the_same_category(setup):
	store, learning = setup
	first = add(store, mail("first"))
	shared_list = add(store, replace(mail("shared-list", sender="other@example.invalid"), headers={"From": "other@example.invalid", "Subject": "Different issue", "List-ID": first.header("List-ID")}))
	unrelated = add(store, replace(mail("unrelated", sender="person@elsewhere.invalid"), headers={"From": "person@elsewhere.invalid", "Subject": "Another newsletter", "List-ID": "different.example.invalid"}))
	for item in (first, shared_list, unrelated):
		store.execute("INSERT INTO predictions(id,message_id,valid) VALUES(?,?,1)", (item.id, item.id))
	learning.record(first, "keep", category="newsletter")
	assert store.one("SELECT valid FROM predictions WHERE id='first'")["valid"] == 0
	assert store.one("SELECT valid FROM predictions WHERE id='shared-list'")["valid"] == 0
	assert store.one("SELECT valid FROM predictions WHERE id='unrelated'")["valid"] == 1
	plan = store.query("EXPLAIN QUERY PLAN SELECT id FROM messages WHERE id=? OR sender=? OR list_id=?", (first.id, first.sender, first.header("List-ID")))
	assert any("idx_messages_list_id" in row["detail"] for row in plan)


def test_examples_join_current_privacy_state_without_n_plus_one_queries(setup):
	store, learning = setup
	with learning.batch():
		for index in range(120):
			learning.record(add(store, mail(f"example-{index}")), "keep", category="newsletter")
	target = add(store, mail("target"))
	statements = []
	store.conn.set_trace_callback(statements.append)
	try:
		assert learning.examples_for(target)
	finally:
		store.conn.set_trace_callback(None)
	selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
	assert len(selects) == 2
	assert "LEFT JOIN messages" in selects[1]
	store.execute("UPDATE messages SET category='medical' WHERE id LIKE 'example-%'")
	assert not learning.examples_for(target, excluded_categories=["medical"])
	# Deleted current cache rows still retain their historical privacy markings.
	store.execute("UPDATE examples SET payload_json=json_set(payload_json,'$.labels',json('[\"L2\"]'))")
	store.execute("DELETE FROM messages WHERE id LIKE 'example-%'")
	assert not learning.examples_for(target, excluded_labels=["L2"])


@pytest.mark.parametrize("subject", ["", "1234", "Re: 123", "Fw: Fwd: 7", "AB 100"])
def test_blank_or_number_only_subjects_do_not_infer_archive_rules(setup, subject):
	store, learning = setup
	for index in range(3):
		message = replace(mail(f"unsafe-{index}", subject=subject), headers={"From": "news@example.invalid", "Subject": subject})
		learning.record(add(store, message), "archive", category="newsletter")
	assert not learning.pattern_suggestions()
	assert store.one("SELECT support FROM pattern_stats WHERE action='archive'")["support"] == 3


def test_list_identity_supports_archive_suggestion_even_without_a_subject(setup):
	store, learning = setup
	for index in range(3):
		learning.record(add(store, mail(f"known-list-{index}", subject="")), "archive", category="newsletter")
	assert len(learning.pattern_suggestions()) == 1
	assert not learning.pattern_suggestions()[0]["enabled"]


def test_stats_rebuild_only_touched_patterns_and_bulk_event_support_stays_independent(setup):
	store, learning = setup
	with learning.batch():
		for index in range(40):
			learning.record(add(store, mail(f"family-{index}", subject=f"Template family {chr(65 + index)}")), "keep", category="newsletter")
	untouched = template_key(mail("family-0", subject="Template family A"))
	store.execute("UPDATE pattern_stats SET updated_at='untouched-marker' WHERE pattern_key=?", (untouched,))
	statements = []
	store.conn.set_trace_callback(statements.append)
	try:
		with learning.batch():
			for index in range(10):
				learning.record(add(store, mail(f"bulk-{index}", subject=f"Independent digest {index}")), "archive", group_id="one-event", category="newsletter")
	finally:
		store.conn.set_trace_callback(None)
	assert store.one("SELECT updated_at FROM pattern_stats WHERE pattern_key=?", (untouched,))["updated_at"] == "untouched-marker"
	assert store.one("SELECT support FROM pattern_stats WHERE pattern_key=? AND action='archive'", (template_key(mail(subject="Independent digest 1")),))["support"] == 1
	assert "DELETE FROM pattern_stats" not in [sql.strip() for sql in statements]
	assert not any(sql.startswith("SELECT pattern_key,action,group_id") and "WHERE pattern_key IN" not in sql for sql in statements)
	assert sum(sql.startswith("UPDATE predictions SET valid=0") for sql in statements) == 3  # IDs, sender, and list, each once.


def test_domain_rule_sql_preview_and_invalidation_match_subdomains_without_wildcards(setup):
	store, learning = setup
	addresses = ["a@example.invalid", "b@sub.example.invalid", "c@evil-example.invalid", "d@example.invalid.evil"]
	for index, sender in enumerate(addresses):
		message = add(store, mail(str(index), sender=sender))
		store.execute("INSERT INTO predictions(id,message_id,valid) VALUES(?,?,1)", (message.id, message.id))
	rule = {"conditions": {"domain": " @EXAMPLE.invalid "}, "action": "keep"}
	assert learning.rule_preview(rule)["sample_ids"] == ["0", "1"]
	learning.save_rule(rule)
	assert [row["valid"] for row in store.query("SELECT valid FROM predictions ORDER BY id")] == [0, 0, 1, 1]
	assert not learning.examples_for(mail("privacy-target", sender="a@sub.example.invalid"), excluded_domains=["example.invalid"])
	add(store, mail("wild-a", sender="a@foo_bar.invalid"))
	add(store, mail("wild-b", sender="a@fooxbar.invalid"))
	assert learning.rule_preview({"conditions": {"domain": "foo_bar.invalid"}, "action": "keep"})["sample_ids"] == ["wild-a"]


def test_template_rule_preview_at_scale_never_loads_cached_bodies(setup, monkeypatch):
	store, learning = setup
	headers = json.dumps(mail().headers)
	with store.transaction():
		store.conn.executemany("INSERT INTO messages(id,sender,subject,headers_json,internal_ms,body) VALUES(?,?,?,?,?,?)", ((f"scale-{index:05}", "news@example.invalid", "Monthly issue 42", headers, index, "private-cached-body") for index in range(50000)))
	monkeypatch.setattr(store, "get_message", lambda mid: pytest.fail("Template matching must not fetch full message rows"))
	rule = {"conditions": {"sender": "news@example.invalid", "template": template_key(mail())}, "action": "archive"}
	statements = []
	store.conn.set_trace_callback(statements.append)
	try:
		preview = learning.rule_preview(rule, limit=3)
		learning._invalidate_rule_matches(rule["conditions"])
	finally:
		store.conn.set_trace_callback(None)
	assert preview["count"] == 50000
	assert len(preview["sample_ids"]) == 3
	selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
	assert len(selects) == 2
	assert all("body" not in sql.casefold() for sql in selects)


def test_rule_edit_stales_only_automatic_pending_matches_including_old_scope(setup):
	store, learning = setup
	for mid, sender in (("old", "a@old.invalid"), ("new", "b@new.invalid"), ("other", "c@other.invalid")):
		add(store, mail(mid, sender=sender))
		store.execute("INSERT INTO actions(id,message_id,state,approval) VALUES(?,?,'queued','automatic')", (mid, mid))
	store.execute("INSERT INTO actions(id,message_id,state,approval) VALUES('manual','old','queued','manual')")
	store.execute("INSERT INTO actions(id,message_id,state,approval) VALUES('applied','old','applied','automatic')")
	rule_id = learning.save_rule({"conditions": {"sender": "a@old.invalid"}, "action": "keep"})
	store.execute("UPDATE actions SET state='queued' WHERE id='old'")
	learning.save_rule({"id": rule_id, "conditions": {"sender": "b@new.invalid"}, "action": "keep"})
	assert {row["id"]: row["state"] for row in store.query("SELECT id,state FROM actions")} == {"old": "stale", "new": "stale", "other": "queued", "manual": "queued", "applied": "applied"}
