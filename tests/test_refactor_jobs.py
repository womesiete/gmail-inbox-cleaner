"""Characterize durable job outcomes before splitting orchestration handlers."""

from dataclasses import replace

import pytest

from gmail_clean.domain import Config
from gmail_clean.jobs import JobRunner
from test_jobs import ANCHOR, PipelineService, mail


@pytest.mark.parametrize("download_all", [False, True])
def test_pipeline_stage_split_preserves_policy_and_durable_outcomes(tmp_path, download_all):
	messages = [mail("archive"), mail("protected", labels={"INBOX", "STARRED"}), mail("recent", age_days=3)]
	service = PipelineService(tmp_path / "characterize.sqlite", messages,
		Config(timezone="UTC", download_all_bodies=download_all))
	try:
		runner = JobRunner(service)
		job = runner.new()
		result = runner.run(job)
		assert result["state"] == "completed" and result["failed"] == 0
		assert service.store.query("SELECT id,decision FROM messages ORDER BY id") == [
			{"id": "archive", "decision": "archive"}, {"id": "protected", "decision": "keep"},
		]
		assert service.store.query("SELECT message_id,stage,state FROM work_items ORDER BY message_id,stage") == [
			{"message_id": "archive", "stage": "body", "state": "completed"},
			{"message_id": "archive", "stage": "classification", "state": "completed"},
			{"message_id": "archive", "stage": "metadata", "state": "completed"},
			*([{"message_id": "protected", "stage": "body", "state": "completed"}] if download_all else []),
			{"message_id": "protected", "stage": "metadata", "state": "completed"},
		]
		assert service.mailbox.body_fetches == (["archive", "protected"] if download_all else ["archive"])
		assert [call["ids"] for call in service.ai.calls] == [["archive"]]
		assert result["ai_usage"] == {"calls": 1, "estimated_cost": 0.0}
		assert [action["message_id"] for action in service.actions.planned] == ["archive"]
		assert service.store.one("SELECT anchor_utc FROM jobs WHERE id=?", (job,))["anchor_utc"] == ANCHOR.isoformat()
	finally:
		service.store.close()


def test_classification_preparation_split_preserves_model_groups_and_cache(tmp_path):
	messages = [mail("a"), mail("b", labels={"INBOX", "CATEGORY_PROMOTIONS"}), mail("c")]
	config = Config(timezone="UTC", ai_batch_size=4, optional_model_routes={"promotion": "promotion-model"})
	service = PipelineService(tmp_path / "groups.sqlite", messages, config)
	try:
		runner = JobRunner(service)
		assert runner.run(runner.new())["state"] == "completed"
		assert [call["ids"] for call in service.ai.calls] == [["a", "c"], ["b"]]
		assert service.routes == ["", "promotion"]
		assert service.store.query("SELECT decision,count(*) n FROM messages GROUP BY decision") == [{"decision": "archive", "n": 3}]
		service.config = replace(config, archive_threshold=26)
		assert runner.run(runner.new())["state"] == "completed"
		assert len(service.ai.calls) == 2
		assert service.store.one("SELECT count(*) n FROM work_items WHERE state!='completed'")["n"] == 0
	finally:
		service.store.close()
