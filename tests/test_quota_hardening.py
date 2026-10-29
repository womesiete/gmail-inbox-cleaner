"""Concurrent budget changes and ETA samples stay local to current work."""
from dataclasses import replace

import pytest

from gmail_clean.gmail import GmailMailbox, QuotaCapacityChanged, QuotaLimiter
from gmail_clean.domain import Config
from gmail_clean.jobs import JobRunner
from gmail_clean.service import AppService
from test_gmail import FakeGoogle, gmail_message
from test_jobs import PipelineService


def test_lowered_budget_during_wait_is_typed_unsent_without_erasing_spending():
	now = [0.0]
	def advance(seconds):
		now[0] += seconds
		limiter.configure(60)
	limiter = QuotaLimiter(120, clock=lambda: now[0], sleep=advance)
	limiter.acquire(120)
	with pytest.raises(QuotaCapacityChanged) as caught:
		limiter.acquire(100)
	assert caught.value.sent is False and caught.value.kind == "transient"
	assert caught.value.units == 100 and caught.value.limit == 60
	assert limiter.consumed == 120
	limiter.acquire(60)
	assert limiter.consumed == 180 and now[0] == 60


def test_waiting_multipart_request_resplits_unsent_parts_after_budget_reduction():
	now = [0.0]
	def advance(seconds):
		now[0] += seconds
		limiter.configure(60)
	limiter = QuotaLimiter(120, clock=lambda: now[0], sleep=advance)
	limiter.acquire(120)
	google = FakeGoogle()
	google.payloads = {str(index): gmail_message(str(index)) for index in range(3)}
	box = GmailMailbox(google, retrieval_batch_size=50, quota_limiter=limiter)
	results = box.get_messages(["0", "1", "2"], body=True)
	assert all(not isinstance(value, Exception) for value in results.values())
	assert google.batches == [1, 1, 1]
	assert limiter.consumed == 300


def test_budget_reduction_after_success_keeps_completed_parts_and_resizes_suffix():
	now = [0.0]
	def advance(seconds):
		now[0] += seconds
	limiter = QuotaLimiter(120, clock=lambda: now[0], sleep=advance)
	class ChangingGoogle(FakeGoogle):
		def new_batch_http_request(self, callback):
			batch = super().new_batch_http_request(callback)
			execute = batch.execute
			def send():
				execute()
				if len(self.batches) == 1:
					limiter.configure(60)
			batch.execute = send
			return batch
	google = ChangingGoogle()
	google.payloads = {str(index): gmail_message(str(index)) for index in range(5)}
	box = GmailMailbox(google, retrieval_batch_size=50, quota_limiter=limiter)
	results = box.get_messages(["0", "1", "2", "3", "4"], body=True)
	assert all(not isinstance(value, Exception) for value in results.values())
	assert google.batches == [2, 1, 1, 1]
	assert limiter.consumed == 300


def test_stale_workers_cannot_restore_a_budget_above_current_saved_limit(tmp_path):
	owner = AppService(data_dir=tmp_path)
	try:
		with owner.worker() as worker:
			limiter = worker._quota_limiter()
			limiter.acquire(20)
			owner.update_config(replace(owner.config, quota_units_per_minute=60))
			assert limiter.limit == 60  # Existing waits see the change immediately.
			assert worker.config.quota_units_per_minute == 6000
			assert owner._quota_limiter().limit == 60
			assert worker._quota_limiter() is limiter
			assert limiter.limit == 60 and limiter.consumed == 20
			# A Settings task may itself be the worker; the older owner must
			# likewise read that account's saved value, not its own snapshot.
			worker.update_config(replace(worker.config, quota_units_per_minute=120))
			assert owner.config.quota_units_per_minute == 60
			assert owner._quota_limiter().limit == 120
	finally:
		owner.close()


def test_current_owner_invocation_override_applies_to_existing_workers(tmp_path):
	owner = AppService(data_dir=tmp_path)
	try:
		with owner.worker() as worker:
			limiter = worker._quota_limiter()
			owner.set_session_config(quota_units_per_minute=15000)
			assert owner.store.checkpoint("effective_preferences")["quota_units_per_minute"] == 6000
			assert worker._quota_limiter() is limiter and limiter.limit == 15000
	finally:
		owner.close()


def test_demo_shared_limiter_also_uses_saved_budget_instead_of_worker_snapshot(tmp_path):
	owner = AppService(data_dir=tmp_path, demo=True, count=1)
	try:
		with owner.worker() as worker:
			limiter = owner.mailbox.quota_limiter
			owner.update_config(replace(owner.config, quota_units_per_minute=60))
			assert limiter.limit == 60  # Existing waits see the change immediately.
			assert worker.config.quota_units_per_minute == 6000
			assert worker.mailbox.quota_limiter is limiter and limiter.limit == 60
	finally:
		owner.close()


def test_processing_eta_excludes_idle_time_and_earlier_resume_samples(tmp_path, monkeypatch):
	import gmail_clean.jobs as jobs_module
	now = [0.0]
	monkeypatch.setattr(jobs_module.time, "monotonic", lambda: now[0])
	service = PipelineService(tmp_path / "eta.sqlite", [], Config(quota_units_per_minute=60))
	try:
		runner = JobRunner(service)
		job = runner.new()
		runner.save_payload(job, {"discovered": True})
		for index in range(10):
			runner.queue(job, str(index), "classification")
		service.store.execute("UPDATE work_items SET state='completed' WHERE message_id IN ('0','1','2','3')")
		now[0] = 3600  # An idle GUI/runner must not become processing latency.
		assert runner.summary(job)["eta_seconds"] == 12
		samples = []
		def process_stage(job, stage, *args):
			if stage != "classification":
				return True
			now[0] += 10
			row = service.store.one("SELECT id FROM work_items WHERE job_id=? AND state='pending' ORDER BY id LIMIT 1", (job,))
			service.store.execute("UPDATE work_items SET state='completed' WHERE id=?", (row["id"],))
			samples.append(runner.summary(job)["eta_seconds"])
			service.store.execute("UPDATE jobs SET state='paused' WHERE id=?", (job,))
			return False
		monkeypatch.setattr(runner, "process_stage", process_stage)
		runner.run(job)
		assert samples == [50]  # Ten seconds for one new stage, five remaining.
		now[0] += 10000
		assert runner.summary(job)["eta_seconds"] == 10  # Outside a run: quota only.
		runner.run(job)
		assert samples == [50, 40]  # The resume has its own completed-count delta.
		assert runner.summary(job)["eta_seconds"] == 8
	finally:
		service.store.close()
