"""Policy/date tests exercise boundaries and independent safety invariants."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gmail_clean.domain import (
	Assessment, Config, Message, PolicyConflict, Window, content_excluded,
	content_hash, context_hash, eligible, epoch_ms, precondition_hash,
	domain_matches, protections, recommend, resolve_age_cutoff, resolve_window, rule_matches,
	subtract_calendar_months,
)


ANCHOR = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def test_default_timezone_uses_system_zone(monkeypatch):
	monkeypatch.setenv("TZ", "Asia/Tokyo")
	assert Config().timezone == "Asia/Tokyo"


def message(days=90, **changes):
	values = {"id": "m1", "thread_id": "t1", "internal_ms": epoch_ms(ANCHOR - timedelta(days=days)), "labels": {"INBOX", "UNREAD"}, "headers": {"From": "Records <records@example.org>", "Subject": "Completed account record"}, "body": "This is complete; no further action is needed.", "context_version": "ctx-1"}
	values.update(changes)
	return Message(**values)


def assessment(**changes):
	values = {"message_id": "m1", "category": "financial", "suggested_label_ids": ["Label_1"], "importance": "high", "outstanding_action": "none", "urgency": "none", "deadline_utc": None, "keep_in_inbox_score": 50, "uncertainty": "low", "missing_context": False, "evidence_reason": "The sender explicitly confirms completion."}
	values.update(changes)
	return Assessment(**values)


def test_unlimited_default_and_rolling_cutoff_are_frozen_and_exact():
	cfg = Config()
	window = resolve_window(cfg, ANCHOR)
	assert window.earliest_inclusive is None
	assert "after:" not in window.query()
	assert window.latest_exclusive == ANCHOR - timedelta(days=14)
	assert window.contains(epoch_ms(window.latest_exclusive) - 1)
	assert not window.contains(epoch_ms(window.latest_exclusive))
	assert Window.from_dict(window.to_dict()) == window
	assert Window.from_dict(window.to_dict()).anchor_utc == ANCHOR
	assert resolve_window(cfg, ANCHOR + timedelta(days=3)).latest_exclusive != window.latest_exclusive


def test_millisecond_cutoff_is_not_rounded_outward():
	anchor = ANCHOR.replace(microsecond=123456)
	window = resolve_window(Config(latest_days=0), anchor)
	assert window.contains(epoch_ms(anchor))
	assert not window.contains(epoch_ms(anchor) + 1)


def test_fixed_calendar_dates_include_entire_dst_transition_day():
	cfg = Config(timezone="America/New_York", earliest_date="2026-03-08", latest_mode="fixed", latest_date="2026-03-08")
	window = resolve_window(cfg, ANCHOR)
	assert window.earliest_inclusive == datetime(2026, 3, 8, 5, tzinfo=UTC)
	assert window.latest_exclusive == datetime(2026, 3, 9, 4, tzinfo=UTC)
	assert window.latest_exclusive - window.earliest_inclusive == timedelta(hours=23)
	assert window.contains(epoch_ms(window.earliest_inclusive))
	assert not window.contains(epoch_ms(window.earliest_inclusive) - 1)
	assert not window.contains(epoch_ms(window.latest_exclusive))
	assert "America/New_York" in window.describe()


def test_fall_calendar_day_is_twenty_five_hours():
	window = resolve_window(Config(timezone="America/New_York", earliest_date="2026-11-01", latest_mode="fixed", latest_date="2026-11-01"), ANCHOR)
	assert window.latest_exclusive - window.earliest_inclusive == timedelta(hours=25)


@pytest.mark.parametrize("cfg", [Config(earliest_date="2026-2-01"), Config(latest_mode="fixed"), Config(latest_date="2026-02-30", latest_mode="fixed"), Config(earliest_date="2026-10-29"), Config(timezone="not/a_zone"), Config(latest_days=-1), Config(latest_days=True), Config(archive_threshold=70, keep_threshold=70)])
def test_invalid_windows_and_settings_are_rejected(cfg):
	with pytest.raises(ValueError):
		resolve_window(cfg, ANCHOR)


def test_naive_anchor_is_rejected_and_zero_exclusion_is_supported():
	with pytest.raises(ValueError):
		resolve_window(Config(), datetime(2026, 10, 28))
	assert resolve_window(Config(latest_days=0), ANCHOR).latest_exclusive == ANCHOR


def test_two_calendar_months_clamp_and_preserve_local_clock():
	anchor = datetime(2024, 4, 30, 15, 7, 4, 987654, tzinfo=UTC)
	assert subtract_calendar_months(anchor, 2) == datetime(2024, 2, 29, 16, 7, 4, 987654, tzinfo=UTC)
	anchor = datetime(2026, 8, 31, 15, tzinfo=UTC)
	assert subtract_calendar_months(anchor, 2) == datetime(2026, 6, 30, 15, tzinfo=UTC)
	assert subtract_calendar_months(anchor, 2) != anchor - timedelta(days=60)


def test_calendar_subtraction_resolves_nonexistent_and_ambiguous_wall_times():
	# May 10 02:30 EDT -> March 10 02:30 (nonexistent) advances to 03:30.
	assert subtract_calendar_months(datetime(2024, 5, 10, 6, 30, tzinfo=UTC), 2) == datetime(2024, 3, 10, 7, 30, tzinfo=UTC)
	# Jan 3 01:30 EST -> Nov 3 01:30 chooses the earlier fold=0 instant.
	assert subtract_calendar_months(datetime(2025, 1, 3, 6, 30, tzinfo=UTC), 2) == datetime(2024, 11, 3, 5, 30, tzinfo=UTC)


def test_age_cutoff_and_examination_cutoff_are_independent():
	cfg = Config()
	window = resolve_window(cfg, ANCHOR)
	assert window.contains(message(30).internal_ms)
	assert recommend(message(30), assessment(), cfg, window).action == "review"
	assert recommend(message(90), assessment(), cfg, window).action == "archive"
	assert recommend(message(90), assessment(), replace(cfg, age_enabled=False), window).action == "review"
	assert recommend(message(2), assessment(keep_in_inbox_score=0), cfg, window).action == "skip"


def test_age_cutoff_is_strictly_earlier():
	cfg = Config()
	cutoff = resolve_age_cutoff(cfg, ANCHOR)
	window = resolve_window(cfg, ANCHOR)
	at_cutoff = message(internal_ms=epoch_ms(cutoff))
	assert recommend(at_cutoff, assessment(), cfg, window).action == "review"
	assert recommend(replace(at_cutoff, internal_ms=at_cutoff.internal_ms - 1), assessment(), cfg, window).action == "archive"


def test_specific_override_precedes_global_and_conflicts_review():
	cfg = Config(age_enabled=False, category_age_overrides={"financial": {"mode": "custom", "months": 1, "days": 0}})
	assert resolve_age_cutoff(cfg, ANCHOR, "financial") is not None
	assert resolve_age_cutoff(cfg, ANCHOR, "personal") is None
	disabled = replace(cfg, age_enabled=True, category_age_overrides={"financial": {"mode": "disabled"}})
	assert resolve_age_cutoff(disabled, ANCHOR, "financial") is None
	conflicting = replace(cfg, category_age_overrides={"financial": [{"mode": "custom", "months": 1}, {"mode": "disabled"}]})
	with pytest.raises(PolicyConflict):
		resolve_age_cutoff(conflicting, ANCHOR, "financial")
	assert recommend(message(), assessment(), conflicting, resolve_window(conflicting, ANCHOR)).action == "review"


def test_important_completed_mail_archives_without_a_user_label():
	cfg = Config()
	item = message(labels={"INBOX", "IMPORTANT", "UNREAD"})
	assert recommend(item, assessment(importance="critical"), cfg, resolve_window(cfg, ANCHOR)).action == "archive"
	assert recommend(item, None, cfg, resolve_window(cfg, ANCHOR)).action == "review"


@pytest.mark.parametrize("changes", [{"outstanding_action": "reply_needed"}, {"outstanding_action": "waiting_on_other"}, {"outstanding_action": "uncertain"}, {"uncertainty": "high"}, {"missing_context": True}, {"keep_in_inbox_score": 70}])
def test_unresolved_uncertain_or_contradictory_signals_never_archive(changes):
	cfg = Config(label_meanings={"Filed": "reference"})
	item = message(labels={"INBOX", "Filed"})
	assert recommend(item, assessment(**changes), cfg, resolve_window(cfg, ANCHOR)).action == "review"


@pytest.mark.parametrize("item", [message(truncated=True), message(missing_content=True), message(labels={"INBOX", "DRAFT"}), message(labels={"INBOX", "TRASH"}), message(labels={"INBOX", "SPAM"}), message(labels=set())])
def test_scope_and_incomplete_content_block_archive(item):
	cfg = Config()
	assert recommend(item, assessment(keep_in_inbox_score=0), cfg, resolve_window(cfg, ANCHOR)).action in {"review", "skip"}


def test_protections_and_context_scoped_manual_bypass():
	cfg = Config(protected_senders=["records@example.org"])
	item = message(labels={"INBOX", "STARRED"})
	window = resolve_window(cfg, ANCHOR)
	assert protections(item, cfg) == {"starred", "sender:records@example.org"}
	assert recommend(item, assessment(), cfg, window).action == "keep"
	manual = {"action": "archive", "context_version": "ctx-1", "protection_bypass": sorted(protections(item, cfg))}
	assert recommend(item, assessment(), cfg, window, manual).action == "archive"
	new_cfg = replace(cfg, protected_label_ids=["NewProtection"])
	assert recommend(replace(item, labels=item.labels | {"NewProtection"}), assessment(), new_cfg, window, manual).action == "review"
	assert recommend(replace(item, context_version="new-reply"), assessment(), cfg, window, manual).action == "keep"


def test_manual_keep_and_deferral_apply_only_to_unchanged_context():
	cfg = Config()
	window = resolve_window(cfg, ANCHOR)
	manual = {"action": "keep", "context_version": "ctx-1"}
	assert recommend(message(), assessment(), cfg, window, manual).action == "keep"
	assert recommend(message(context_version="new-reply"), assessment(), cfg, window, manual).action == "archive"
	defer = {"action": "defer", "context_version": "ctx-1", "defer_until_utc": (ANCHOR + timedelta(days=1)).isoformat()}
	assert recommend(message(), assessment(), cfg, window, defer).action == "keep"
	assert recommend(message(), assessment(), cfg, resolve_window(cfg, ANCHOR + timedelta(days=2)), defer).action == "archive"


def test_conflicting_rules_and_unapproved_inference_require_review():
	cfg = Config()
	window = resolve_window(cfg, ANCHOR)
	archive = {"enabled": True, "conditions": {"sender": "records@example.org", "subject_contains": "Completed"}, "action": "archive", "source": "explicit"}
	keep = {**archive, "action": "keep"}
	assert recommend(message(), assessment(), cfg, window, rules=[archive, keep]).action == "review"
	assert recommend(message(), None, cfg, window, rules=[archive]).action == "archive"
	assert recommend(message(), None, cfg, window, rules=[{**archive, "source": "inferred"}]).action == "review"
	assert recommend(message(), assessment(outstanding_action="reply_needed"), cfg, window, rules=[archive]).action == "review"


def test_score_thresholds_and_auto_label_policy():
	cfg = Config(age_enabled=False, auto_label=True)
	window = resolve_window(cfg, ANCHOR)
	assert recommend(message(), assessment(keep_in_inbox_score=24), cfg, window).action == "archive"
	assert recommend(message(), assessment(keep_in_inbox_score=25), cfg, window).action == "review"
	assert recommend(message(), assessment(keep_in_inbox_score=69), cfg, window).action == "review"
	assert recommend(message(), assessment(keep_in_inbox_score=0), cfg, window).label_ids == ("Label_1",)
	assert recommend(message(), assessment(keep_in_inbox_score=0), replace(cfg, auto_label=False), window).label_ids == ()


def test_hashes_ignore_header_order_but_include_content_and_context():
	first = message()
	second = replace(first, headers={"subject": first.subject, "from": first.header("From")})
	assert content_hash(first) == content_hash(second)
	assert content_hash(first) != content_hash(replace(first, body="An outstanding request"))
	assert context_hash([first]) == context_hash([replace(first, labels=first.labels | {"UNREAD", "IMPORTANT"})])
	assert context_hash([first]) != context_hash([first, message(id="m2", internal_ms=first.internal_ms + 1)])
	assert context_hash([first]) != context_hash([first], True)
	assert precondition_hash(first, "policy1") != precondition_hash(first, "policy2")


def test_roundtrip_models_preserves_infinite_bound_and_label_identity():
	cfg = Config.from_dict({"label_meanings": {"Label_1": "reference"}, "future_preference": 3})
	assert Config.from_dict(cfg.to_dict()) == cfg
	assert cfg.extras["future_preference"] == 3
	item = message()
	assert Message.from_dict(item.to_dict()) == item
	assert Assessment.from_dict(assessment().to_dict()) == assessment()
	assert content_excluded(item, Config(excluded_domains=["example.org"]))
	assert eligible(item, resolve_window(cfg, ANCHOR))


@pytest.mark.parametrize("sender,target,expected", [
	("person@sub.Example.ORG", " @EXAMPLE.org ", True),
	("sub.example.org", "example.org", True),
	("person@example.org", "example.org", True),
	("person@evil-example.org", "example.org", False),
	("person@example.org.evil.invalid", "example.org", False),
	("person@subexample.org", "example.org", False),
	("", "example.org", False), ("person@example.org", " @ ", False),
])
def test_domain_matcher_has_one_exact_and_subdomain_meaning(sender, target, expected):
	assert domain_matches(sender, target) is expected


def test_domain_rules_protections_and_exclusions_share_subdomain_scope():
	cfg = Config(protected_domains=["example.org"], excluded_domains=["example.org"])
	rule = {"enabled": True, "conditions": {"domain": " @Example.Org "}, "action": "keep"}
	for sender, expected in (("news@sub.example.org", True), ("news@example.org", True), ("news@evil-example.org", False)):
		item = message(headers={"From": sender})
		assert bool(protections(item, cfg)) is expected
		assert content_excluded(item, cfg) is expected
		assert rule_matches(rule, item) is expected
