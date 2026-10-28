"""Cross-component regressions for live-state privacy and undo ownership.

All mailbox effects are isolated synthetic DemoMailbox changes. No Google service,
provider request, private module, or credentials are used by these tests.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from gmail_clean.demo import DEMO_ACCOUNT, DemoMailbox
from gmail_clean.domain import Assessment, Config, Message, context_hash, epoch_ms, resolve_window
from gmail_clean.service import AppService

ANCHOR = datetime(2026, 10, 28, 12, tzinfo=UTC)


class RecordedAI:
	def __init__(self):
		self.calls = []

	def assess(self, messages, labels, examples=(), **kwargs):
		self.calls.append([message.id for message in messages])
		return SimpleNamespace(assessments={message.id: Assessment(message.id, "financial", [], "high", "none", "none", None, 10, "low", False, "Explicitly completed financial record.") for message in messages}, errors={}, telemetry={})

	def close(self):
		return None


class ReceiptRaceMailbox(DemoMailbox):
	"""Another synthetic client removes/readds a label before receipt checkpoint."""
	def __init__(self):
		super().__init__(count=0, anchor=ANCHOR)
		self.intervened = False

	def profile(self):
		if self.mutations and not self.intervened:
			self.intervened = True
			super().modify(["target"], [], ["label_records"])
			super().modify(["target"], ["label_records"], [])
		return super().profile()


def mail(mid="target", age_days=100, *, labels=None, thread_id=None):
	return Message(mid, thread_id or mid, epoch_ms(ANCHOR - timedelta(days=age_days)),
		labels=set(labels if labels is not None else {"INBOX", "UNREAD"}),
		headers={"From": "records@example.invalid", "Subject": "Completed record"},
		body="The matter is complete and no response is required.", account_id=DEMO_ACCOUNT)


def make_service(tmp_path, mailbox=None, config=None):
	mailbox = mailbox or DemoMailbox(count=0, anchor=ANCHOR)
	ai = RecordedAI()
	service = AppService(data_dir=tmp_path, demo=True, mailbox=mailbox,
		config=config or Config(dry_run=False), clock=lambda: ANCHOR,
		ai_factory=lambda *args, **kwargs: ai)
	return service, ai


def seed(service, message):
	service.mailbox.add_message(message)
	message = replace(message, labels=set(message.labels))
	message.context_version = context_hash(service.mailbox.thread_metadata(message.thread_id), service.config.sent_context_enabled)
	service.store.upsert_message(message)
	return message


def test_manual_command_cannot_bypass_a_new_undisplayed_star(tmp_path):
	service, _ = make_service(tmp_path)
	try:
		seed(service, mail())  # The selected/displayed cache is unstarred.
		service.mailbox.messages["target"].labels.add("STARRED")
		service.manual(["target"], "archive")
		assert "INBOX" in service.mailbox.messages["target"].labels
		assert not service.store.one("SELECT id FROM actions WHERE state='applied'")
		assert not service.mailbox.mutations
	finally:
		service.close()


def test_tightened_current_window_treats_newer_excluded_reply_as_incomplete_context(tmp_path):
	service, ai = make_service(tmp_path, config=Config())
	try:
		seed(service, mail("older", thread_id="conversation"))
		seed(service, mail("reply", age_days=40, thread_id="conversation"))
		job_id = service.new_job()  # Frozen latest cutoff excludes only 14 days.
		service.update_config(Config.from_dict({**service.config.to_dict(), "latest_days": 60}))
		service.run_job(job_id)
		assert service.store.one("SELECT decision FROM messages WHERE id='older'")["decision"] == "review"
		assert not ai.calls
		assert not service.mailbox.body_reads
	finally:
		service.close()


def test_undo_cannot_claim_a_user_readded_label_hidden_before_receipt_checkpoint(tmp_path):
	mailbox = ReceiptRaceMailbox()
	service, _ = make_service(tmp_path, mailbox=mailbox)
	try:
		message = seed(service, mail())
		window = resolve_window(service.config, ANCHOR)
		aid = service.actions.plan("", message, "label", ["label_records"], "Selected reference label", "manual", window)
		service.apply_actions([aid])
		assert mailbox.intervened
		assert "label_records" in mailbox.messages["target"].labels
		before_undo_calls = len(mailbox.mutations)
		service.undo([aid])
		assert "label_records" in mailbox.messages["target"].labels
		assert len(mailbox.mutations) == before_undo_calls
		assert service.store.one("SELECT id FROM actions WHERE state='conflict'")
	finally:
		service.close()


def test_undo_preserves_a_newly_attention_meaning_label_including_spaced_value(tmp_path):
	service, _ = make_service(tmp_path)
	try:
		message = seed(service, mail())
		window = resolve_window(service.config, ANCHOR)
		aid = service.actions.plan("", message, "label", ["label_records"], "Selected reference label", "manual", window)
		assert service.apply_actions([aid])["applied"] == 1
		service.update_config(Config.from_dict({**service.config.to_dict(), "label_meanings": {"label_records": "requires attention"}}))
		service.undo([aid])
		assert "label_records" in service.mailbox.messages["target"].labels
		assert service.store.one("SELECT id FROM actions WHERE undo_of=? AND state='conflict'", (aid,))
	finally:
		service.close()


@pytest.mark.parametrize("change", ["archive", "private_label"])
def test_paused_body_queue_refreshes_live_eligibility_before_fetching_content(tmp_path, change):
	config = Config(excluded_label_ids=["label_personal"])
	service, ai = make_service(tmp_path, config=config)
	try:
		message = seed(service, mail())
		# Queue body work using only metadata, as if paused between stages.
		service.store.execute("UPDATE messages SET body='',missing_content=1,payload_json='{}' WHERE id=?", (message.id,))
		job_id = service.new_job()
		window = resolve_window(service.config, ANCHOR)
		service.jobs.queue(job_id, message.id, "body")
		if change == "archive":
			service.mailbox.messages[message.id].labels.discard("INBOX")
		else:
			service.mailbox.messages[message.id].labels.add("label_personal")
		service.jobs.process_stage(job_id, "body", window, config, None, None)
		assert service.mailbox.body_reads == []
		assert not ai.calls
	finally:
		service.close()


def test_manual_displayed_protection_bypass_survives_replan_for_unchanged_context(tmp_path):
	service, ai = make_service(tmp_path, config=Config(dry_run=True))
	try:
		seed(service, mail(labels={"INBOX", "STARRED", "UNREAD"}))
		service.manual(["target"], "archive")
		override = service.override(service.store.get_message("target"))
		assert override["protection_bypass"] == ["starred"]
		service.update_config(Config.from_dict({**service.config.to_dict(), "dry_run": False}))
		job_id = service.new_job()
		service.run_job(job_id)
		service.apply_actions(job_id=job_id)
		assert "INBOX" not in service.mailbox.messages["target"].labels
		assert "STARRED" in service.mailbox.messages["target"].labels
		assert not ai.calls
	finally:
		service.close()
