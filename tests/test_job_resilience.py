"""Durable outage recovery uses the existing recording mailbox and fake clock."""

from datetime import timedelta

import pytest

from gmail_clean.gmail import GmailError
from gmail_clean.jobs import JobRunner
from test_jobs import ANCHOR, PipelineService, mail


@pytest.mark.parametrize("kind", ["network", "transient"])
def test_ten_minute_transport_outage_never_exhausts_item_attempts(tmp_path, monkeypatch, kind):
	service = PipelineService(tmp_path / "outage.sqlite", [mail("target")])
	try:
		outage_end = ANCHOR + timedelta(minutes=10)
		original = service.mailbox.get_messages
		def retrieve(ids, body=False):
			if service.now < outage_end:
				return {mid: GmailError(kind, "Synthetic ten-minute outage") for mid in ids}
			return original(ids, body)
		monkeypatch.setattr(service.mailbox, "get_messages", retrieve)
		runner = JobRunner(service)
		job = runner.new()
		result = runner.run(job)
		resumes = 0
		while result["state"] != "completed":
			assert result["state"] == "waiting" and result["failed"] == 0
			deadline = runner.retry_deadline(job)
			assert deadline is not None and deadline > service.now
			assert deadline - service.now <= timedelta(seconds=300)
			service.now = deadline
			resumes += 1
			assert resumes < 20
			result = runner.run(job)
		assert resumes > service.config.max_retries
		assert service.now >= outage_end
		assert result["failed"] == result["retry"] == result["pending"] == 0
		assert {row["attempts"] for row in service.store.query("SELECT attempts FROM work_items")} == {0}
		assert "transport_failures" not in runner.payload(job)
		assert [call["ids"] for call in service.ai.calls] == [["target"]]
	finally:
		service.store.close()


@pytest.mark.parametrize("kind", ["auth", "permission", "alternating"])
def test_repeated_poison_item_pauses_fail_only_that_item_after_explicit_resumes(tmp_path, kind):
	service = PipelineService(tmp_path / "poison.sqlite", [mail("bad"), mail("good")])
	try:
		service.config.retrieval_batch_size = 1
		service.mailbox.retrieval_errors["bad"] = GmailError(kind, "Synthetic item-specific access problem")
		runner = JobRunner(service)
		job = runner.new()
		for resume in range(runner.POISON_RESUME_LIMIT):
			current_kind = ("auth" if resume % 2 == 0 else "permission") if kind == "alternating" else kind
			service.mailbox.retrieval_errors["bad"] = GmailError(current_kind, "Synthetic item-specific access problem")
			result = runner.run(job)
			assert result["state"] == "paused" and result["failed"] == 0
			assert runner.retry_deadline(job) is None
		if kind == "alternating":
			service.mailbox.retrieval_errors["bad"] = GmailError("permission", "Synthetic item-specific access problem")
		result = runner.run(job)
		assert result["state"] == "completed" and result["failed"] == 1
		assert service.store.one("SELECT state FROM work_items WHERE message_id='bad'")["state"] == "failed"
		assert {row["state"] for row in service.store.query("SELECT state FROM work_items WHERE message_id='good'")} == {"completed"}
		assert [call["ids"] for call in service.ai.calls] == [["good"]]
	finally:
		service.store.close()


def test_transport_success_resets_streak_and_retains_other_item_retry(tmp_path):
	service = PipelineService(tmp_path / "reset.sqlite", [mail("bad"), mail("good")])
	try:
		runner = JobRunner(service)
		job = runner.new()
		for mid in ("bad", "good"):
			runner.queue(job, mid)
		row = service.store.one("SELECT * FROM work_items WHERE message_id='bad'")
		runner.fail(job, row, GmailError("network", "Synthetic outage"), service.config)
		assert runner.payload(job)["transport_failures"] == 1
		runner.done(job, "good", "metadata")
		assert "transport_failures" not in runner.payload(job)
		assert "backoff" not in runner.payload(job)
		assert service.store.one("SELECT state FROM work_items WHERE message_id='bad'")["state"] == "retry"
	finally:
		service.store.close()


def test_typed_transport_interruption_pauses_without_retry_or_attempt(tmp_path):
	service = PipelineService(tmp_path / "interrupted.sqlite", [mail("target")])
	try:
		runner = JobRunner(service)
		job = runner.new()
		runner.queue(job, "target")
		row = service.store.one("SELECT * FROM work_items WHERE job_id=?", (job,))
		runner.fail(job, row, GmailError("interrupted", "Stopped while waiting for quota"), service.config)
		assert service.store.one("SELECT state,attempts FROM work_items WHERE id=?", (row["id"],)) == {"state": "pending", "attempts": 0}
		assert runner.summary(job)["state"] == "paused"
		assert runner.payload(job)["pause"]["origin"] == "user"
		assert runner.retry_deadline(job) is None
	finally:
		service.store.close()


@pytest.mark.parametrize("interrupted_recovery", [False, True])
def test_rate_limit_shrink_recovers_after_clean_batches_and_survives_runner_restart(tmp_path, monkeypatch, interrupted_recovery):
	messages = [mail(f"target-{index:03d}") for index in range(60)]
	service = PipelineService(tmp_path / "adaptive.sqlite", messages)
	try:
		service.config.retrieval_batch_size = 8
		original = service.mailbox.get_messages
		metadata_sizes = []
		first = True
		def retrieve(ids, body=False):
			nonlocal first
			stage = service.store.one("SELECT stage FROM jobs ORDER BY created_at DESC LIMIT 1")["stage"]
			if stage == "metadata":
				metadata_sizes.append(len(ids))
			if first:
				first = False
				return {mid: GmailError("rate_limit", "Synthetic rate-limit burst") for mid in ids}
			result = original(ids, body)
			if interrupted_recovery and stage == "metadata" and len(metadata_sizes) == 3:
				result[ids[0]] = GmailError("data", "One malformed result interrupts a clean recovery streak")
			return result
		monkeypatch.setattr(service.mailbox, "get_messages", retrieve)
		runner = JobRunner(service)
		job = runner.new()
		assert runner.run(job)["state"] == "waiting"
		assert runner.payload(job)["batch_sizes"]["metadata"] == 4
		service.now = runner.retry_deadline(job)
		# The new runner has no in-memory adaptation state; SQLite owns the limit.
		runner = JobRunner(service)
		checkpoints = []
		def progress(_):
			checkpoints.append(runner.payload(job))
		result = runner.run(job, progress=progress)
		assert result["state"] == "completed" and result["failed"] == int(interrupted_recovery)
		expected = [8, 4, 4, 4, 4, 4, 8] if interrupted_recovery else [8, 4, 4, 4, 8]
		assert metadata_sizes[:len(expected)] == expected
		assert any(payload.get("batch_clean", {}).get("metadata") == 1 for payload in checkpoints)
		assert max(metadata_sizes) == service.config.retrieval_batch_size
		assert "batch_sizes" not in runner.payload(job)
		assert "batch_clean" not in runner.payload(job)
	finally:
		service.store.close()


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, OverflowError])
def test_untyped_discovery_failure_keeps_a_bounded_system_retry_budget(tmp_path, monkeypatch, error_type):
	service = PipelineService(tmp_path / "unexpected.sqlite", [mail("target")])
	try:
		service.config.max_retries = 1
		def fail_discovery(*args, **kwargs):
			raise error_type("Synthetic local discovery failure")
		monkeypatch.setattr(service.mailbox, "list_messages", fail_discovery)
		runner = JobRunner(service)
		job = runner.new()
		first = runner.run(job)
		assert first["state"] == "paused" and first["pause"]["kind"] == "unexpected"
		service.now = runner.retry_deadline(job)
		result = runner.run(job)
		assert result["state"] == "paused"
		assert result["pause"]["attempts"] == 2
		assert runner.retry_deadline(job) is None
		assert "transport_failures" not in runner.payload(job)
	finally:
		service.store.close()


@pytest.mark.parametrize("stage", ["discovery", "metadata"])
@pytest.mark.parametrize("kind,delay", [("network", float("inf")), ("transient", 1e308), ("rate_limit", float("nan")), ("quota", -1), ("network", "invalid"), ("network", True)])
def test_unusable_provider_delay_requires_manual_resume_without_an_earlier_retry(tmp_path, monkeypatch, stage, kind, delay):
	service = PipelineService(tmp_path / "bad-delay.sqlite", [mail("target")])
	try:
		error = GmailError(kind, "Synthetic provider delay failure", retry_after=delay)
		if stage == "discovery":
			def fail_discovery(*args, **kwargs):
				raise error
			monkeypatch.setattr(service.mailbox, "list_messages", fail_discovery)
		else:
			service.mailbox.retrieval_errors["target"] = error
		runner = JobRunner(service)
		job = runner.new()
		result = runner.run(job)
		assert result["state"] == "paused" and result["pause"]["kind"] == "retry_metadata"
		assert result["pause"]["due"] is None
		assert runner.retry_deadline(job) is None
		assert not service.ai.calls
		if stage == "metadata":
			assert service.store.one("SELECT state,attempts FROM work_items WHERE job_id=?", (job,)) == {"state": "pending", "attempts": 0}
	finally:
		service.store.close()


def test_invalid_delay_flag_survives_an_exception_boundary_that_removed_the_value(tmp_path):
	service = PipelineService(tmp_path / "invalid-flag.sqlite", [mail("target")])
	try:
		error = GmailError("transient", "Synthetic invalid delay marker")
		error.retry_after_invalid = True
		service.mailbox.retrieval_errors["target"] = error
		runner = JobRunner(service)
		job = runner.new()
		result = runner.run(job)
		assert result["state"] == "paused" and result["pause"]["kind"] == "retry_metadata"
		assert runner.retry_deadline(job) is None
	finally:
		service.store.close()
