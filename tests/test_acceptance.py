"""Offline cross-component acceptance workflows, with no account/provider access."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3

from gmail_clean.domain import Config, Window, eligible
from gmail_clean.service import AppService


ANCHOR = datetime(2026, 10, 28, 12, tzinfo=UTC)


def test_offline_pipeline_uses_explicit_eligible_ids_and_never_mutates_in_simulation(tmp_path):
	service = AppService(data_dir=tmp_path, demo=True, count=60, clock=lambda: ANCHOR)
	try:
		original = {mid: set(message.labels) for mid, message in service.mailbox.messages.items()}
		job_id = service.new_job()
		summary = service.run_job(job_id)
		assert summary["state"] == "completed", service.store.query("SELECT * FROM errors")
		assert summary["failed"] == 0
		job = service.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
		window = Window.from_dict(json.loads(job["window_json"]))
		assert window.earliest_inclusive is None
		assert window.latest_exclusive == ANCHOR - timedelta(days=14)
		assert service.store.one("SELECT count(*) n FROM predictions")["n"] > 0
		assert service.store.one("SELECT count(*) n FROM actions")["n"] > 0
		assert service.mailbox.mutations == []
		assert original == {mid: set(message.labels) for mid, message in service.mailbox.messages.items()}
		assert all(eligible(service.mailbox.messages[mid], window) for mid in service.mailbox.body_reads)
		for row in service.store.query("SELECT message_id FROM actions"):
			assert eligible(service.mailbox.messages[row["message_id"]], window)
		# Synthetic old request + recent excluded reply share a conversation.
		old_id, recent_id = f"d{30:015x}", f"d{31:015x}"
		assert service.store.one("SELECT decision FROM messages WHERE id=?", (old_id,))["decision"] == "review"
		assert recent_id not in service.mailbox.body_reads
		assert service.store.one("SELECT count(*) n FROM actions WHERE message_id=?", (recent_id,))["n"] == 0
	finally:
		service.close()


def test_learning_pause_keeps_manual_override_and_does_not_retroactively_include_feedback(tmp_path):
	config = Config(learning_enabled=False)
	service = AppService(data_dir=tmp_path, demo=True, count=36, clock=lambda: ANCHOR, config=config)
	try:
		service.run_job(service.new_job())
		message_id = f"d{2:015x}"  # Completed important insurance correspondence.
		service.manual([message_id], "keep", reason="Still checking this record")
		row = service.store.one("SELECT * FROM manual_overrides WHERE message_id=?", (message_id,))
		assert row["action"] == "keep"
		assert service.store.one("SELECT count(*) n FROM examples")["n"] == 0
		feedback = service.store.one("SELECT * FROM feedback WHERE message_id=?", (message_id,))
		assert feedback["include_learning"] == 0
		assert service.mailbox.mutations == []
		service.update_config(Config.from_dict({**service.config.to_dict(), "learning_enabled": True}))
		service.run_job(service.new_job())
		assert service.store.one("SELECT decision FROM messages WHERE id=?", (message_id,))["decision"] == "keep"
		assert service.store.one("SELECT count(*) n FROM examples")["n"] == 0
		assert service.store.one("SELECT include_learning FROM feedback WHERE id=?", (feedback["id"],))["include_learning"] == 0
		assert service.store.one("SELECT count(*) n FROM actions WHERE message_id=? AND state IN ('queued','proposed')", (message_id,))["n"] == 0
	finally:
		service.close()


def test_deferral_finishes_without_gmail_event_and_survives_service_restart(tmp_path):
	now = [ANCHOR]
	service = AppService(data_dir=tmp_path, demo=True, count=36, clock=lambda: now[0])
	message_id = f"d{2:015x}"
	try:
		service.run_job(service.new_job())
		service.manual([message_id], "defer", defer_until=(ANCHOR + timedelta(days=1)).isoformat())
		history_id = service.mailbox.profile()["historyId"]
		assert service.store.one("SELECT defer_until_utc FROM messages WHERE id=?", (message_id,))["defer_until_utc"]
	finally:
		service.close()
	service = AppService(data_dir=tmp_path, demo=True, count=36, clock=lambda: now[0])
	try:
		now[0] += timedelta(hours=12)
		service.run_job(service.new_job())
		assert service.store.one("SELECT decision FROM messages WHERE id=?", (message_id,))["decision"] == "keep"
		now[0] += timedelta(days=1)
		service.run_job(service.new_job())
		assert service.mailbox.profile()["historyId"] == history_id
		assert service.store.one("SELECT defer_until_utc FROM messages WHERE id=?", (message_id,))["defer_until_utc"] is None
		assert service.store.one("SELECT * FROM manual_overrides WHERE message_id=?", (message_id,)) is None
		assert service.store.one("SELECT decision FROM messages WHERE id=?", (message_id,))["decision"] == "archive"
	finally:
		service.close()


def test_backup_and_retention_preserve_feedback_plan_and_undo_ledger(tmp_path):
	service = AppService(data_dir=tmp_path / "state", demo=True, count=36, clock=lambda: ANCHOR)
	try:
		service.run_job(service.new_job())
		message_id = f"d{2:015x}"
		service.manual([message_id], "archive", reason="completed")
		before_actions = service.store.one("SELECT count(*) n FROM actions")["n"]
		before_feedback = service.store.one("SELECT count(*) n FROM feedback")["n"]
		backup_path = tmp_path / "private-backup.sqlite"
		assert service.backup(backup_path) == str(backup_path)
		with sqlite3.connect(backup_path) as backup:
			assert backup.execute("SELECT count(*) FROM actions").fetchone()[0] == before_actions
			assert backup.execute("SELECT count(*) FROM feedback").fetchone()[0] == before_feedback
			assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
		service.store.execute("UPDATE messages SET updated_at='2000-01-01T00:00:00+00:00' WHERE body!=''")
		result = service.retention()
		assert result["bodies_removed"] > 0
		assert service.store.one("SELECT count(*) n FROM messages WHERE body!=''")["n"] == 0
		assert service.store.one("SELECT count(*) n FROM actions")["n"] == before_actions
		assert service.store.one("SELECT count(*) n FROM feedback")["n"] == before_feedback
		# Learning examples have separate retention/reset controls from body cache.
		assert service.store.one("SELECT count(*) n FROM examples")["n"] > 0
	finally:
		service.close()
