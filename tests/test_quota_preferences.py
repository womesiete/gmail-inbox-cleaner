"""User quota budgets retain shared rolling history and visible progress."""
from dataclasses import replace

import pytest

from gmail_clean.app import main, parser
from gmail_clean.demo import DemoMailbox
from gmail_clean.domain import Config
from gmail_clean.gmail import GmailMailbox, QuotaLimiter
from gmail_clean.gui_preferences import SettingsEditor
from gmail_clean.service import AppService
from test_gmail import FakeGoogle, gmail_message
from test_jobs import ANCHOR


@pytest.mark.parametrize("value", [59, 1_000_001, -1, True, 6000.5])
def test_invalid_quota_budgets_are_rejected(value):
	with pytest.raises(ValueError, match="quota_units_per_minute"):
		Config(quota_units_per_minute=value).validate()


def test_default_budget_cli_validation_and_settings_round_trip(qtbot):
	assert Config().quota_units_per_minute == 6000
	assert parser().parse_args(["--quota-units-per-minute", "15000"]).quota_units_per_minute == 15000
	with pytest.raises(SystemExit) as result:
		main(["--quota-units-per-minute", "10"])
	assert result.value.code == 2
	editor = SettingsEditor(Config())
	qtbot.addWidget(editor)
	editor.controls["quota_units_per_minute"][0].setValue(15000)
	assert editor.read_config().quota_units_per_minute == 15000


def test_reconfiguring_budget_retains_spending_and_splits_small_batches():
	now = [0.0]
	def advance(seconds):
		now[0] += seconds
	limiter = QuotaLimiter(120, clock=lambda: now[0], sleep=advance)
	limiter.acquire(100)
	limiter.configure(60)
	assert limiter.consumed == 100
	service = FakeGoogle()
	service.payloads = {str(index): gmail_message(str(index)) for index in range(3)}
	box = GmailMailbox(service, retrieval_batch_size=50, quota_limiter=limiter)
	results = box.get_messages(["0", "1", "2"], body=True)
	assert all(not isinstance(value, Exception) for value in results.values())
	assert service.batches == [1, 1, 1]
	assert limiter.consumed == 280 and now[0] >= 180


def test_progress_and_summary_include_modeled_units_and_eta(tmp_path):
	box = DemoMailbox(count=20, anchor=ANCHOR)
	service = AppService(mailbox=box, demo=True, config=Config(ai_module="gmail_clean.demo_ai", quota_units_per_minute=15000),
		data_dir=tmp_path, clock=lambda: ANCHOR)
	try:
		assert service.mailbox.quota_limiter.limit == 15000
		job = service.new_job()
		progress = []
		result = service.run_job(job, progress=progress.append)
		assert result["state"] == "completed" and result["failed"] == 0
		assert result["quota_units_consumed"] == result["quota_usage"]["total"] > 0
		assert result["eta_seconds"] == 0
		assert progress and all("quota " in item and "ETA ~" in item for item in progress)
	finally:
		service.close()


def test_resource_owner_reuses_limiter_when_budget_changes(tmp_path):
	service = AppService(data_dir=tmp_path)
	try:
		first = service._quota_limiter()
		first.acquire(20)
		service.update_config(replace(service.config, quota_units_per_minute=15000))
		assert service._quota_limiter() is first
		assert first.limit == 15000 and first.consumed == 20
	finally:
		service.close()
