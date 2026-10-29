"""Legacy normalized text is refetched before the new MIME safety decisions."""

import base64
from dataclasses import replace
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest

from gmail_clean.actions import ActionExecutor
from gmail_clean.ai import PROMPT_VERSION
from gmail_clean.domain import Config, eligible, resolve_window
from gmail_clean.jobs import JobRunner
from gmail_clean.mime import BODY_PARSER_VERSION, parse_body
from gmail_clean.runtime import encode, stamp
from gmail_clean.storage import Store
from test_actions import MemoryMailbox
from test_jobs import ACCOUNT, ANCHOR, PipelineService, RecordingMailbox, mail


def make_legacy(path):
	with Store(path, ACCOUNT) as store:
		store.upsert_message(mail(body="Visible text. Legacy hidden footer."))
		store.execute("INSERT INTO feedback(id,message_id,action,scope,created_at) VALUES('feedback','old','keep','message','2026-10-01')")
		store.execute("INSERT INTO examples(id,feedback_id,message_id,action,body,reason,created_at,payload_json) VALUES('example','feedback','old','keep','Legacy hidden footer.','Reviewed reason','2026-10-01',?)", (encode({"body": "Legacy hidden footer.", "scope": "message"}),))
		store.execute("INSERT INTO predictions(id,message_id,valid) VALUES('prediction','old',1)")
		for state in ("queued", "proposed", "retry", "applied", "reconciled", "uncertain", "sending"):
			store.execute("INSERT INTO actions(id,message_id,state) VALUES(?, 'old', ?)", (state, state))
		store.execute("INSERT INTO actions(id,message_id,state,approval) VALUES('manual','old','queued','manual')")
		store.execute("INSERT INTO actions(id,message_id,state,undo_of) VALUES('undo','old','queued','applied')")
		store.execute("INSERT INTO attempts(id,action_id,state) VALUES('receipt','uncertain','sending')")
		store.upsert_message(mail("manual-message", body="Reviewed manual text"))
		store.execute("INSERT INTO manual_overrides(message_id,action,context_version,payload_json) VALUES('manual-message','keep','reviewed-context',?)", (encode({"scope": "message", "reason": "Explicit reviewed keep"}),))
		store.execute("UPDATE messages SET decision='keep' WHERE id='manual-message'")
		store.execute("DELETE FROM checkpoints WHERE key='body_parser_version'")


def test_reopen_invalidates_only_legacy_body_evidence_and_pending_automatic_plans(tmp_path):
	path = tmp_path / "legacy.sqlite"
	make_legacy(path)
	with Store(path, ACCOUNT) as store:
		message = store.get_message("old")
		assert message.body == "" and message.missing_content
		assert not message.content_hash
		assert store.one("SELECT body_cached_at FROM messages WHERE id='old'")["body_cached_at"] == ""
		assert store.one("SELECT decision FROM messages WHERE id='old'")["decision"] == "pending"
		assert store.checkpoint("body_parser_version") == BODY_PARSER_VERSION
		assert store.one("SELECT valid FROM predictions")["valid"] == 0
		example = store.one("SELECT * FROM examples")
		assert not example["body"] and "body" not in json.loads(example["payload_json"])
		assert example["reason"] == "Reviewed reason"
		assert json.loads(example["payload_json"])["scope"] == "message"
		assert store.one("SELECT action,scope FROM feedback") == {"action": "keep", "scope": "message"}
		states = {row["id"]: row["state"] for row in store.query("SELECT id,state FROM actions")}
		assert {states[state] for state in ("queued", "proposed", "retry")} == {"stale"}
		assert all(states[state] == state for state in ("applied", "reconciled", "uncertain", "sending"))
		assert states["manual"] == states["undo"] == "queued"
		assert store.one("SELECT action_id,state FROM attempts") == {"action_id": "uncertain", "state": "sending"}
		assert store.one("SELECT decision FROM messages WHERE id='manual-message'")["decision"] == "keep"
		override = store.one("SELECT action,context_version,payload_json FROM manual_overrides")
		assert override["action"] == "keep" and override["context_version"] == "reviewed-context"
		assert json.loads(override["payload_json"]) == {"scope": "message", "reason": "Explicit reviewed keep"}
		store.upsert_message(mail(body="Fresh parsed text"))
		store.execute("INSERT INTO actions(id,message_id,state) VALUES('new','old','queued')")
	with Store(path, ACCOUNT) as current:
		assert current.get_message("old").body == "Fresh parsed text"
		assert current.one("SELECT state FROM actions WHERE id='new'")["state"] == "queued"


def test_interrupted_parser_upgrade_rolls_back_every_invalidation(tmp_path, monkeypatch):
	path = tmp_path / "legacy.sqlite"
	make_legacy(path)
	setter = Store.set_checkpoint
	def interrupted(self, key, value):
		if key == "body_parser_version":
			raise OSError("Synthetic checkpoint write failure")
		return setter(self, key, value)
	with monkeypatch.context() as patch:
		patch.setattr(Store, "set_checkpoint", interrupted)
		with pytest.raises(OSError, match="checkpoint"):
			Store(path, ACCOUNT)
	with sqlite3.connect(path) as connection:
		assert connection.execute("SELECT body FROM messages WHERE id='old'").fetchone()[0] == "Visible text. Legacy hidden footer."
		assert connection.execute("SELECT valid FROM predictions").fetchone()[0] == 1
		assert connection.execute("SELECT state FROM actions WHERE id='queued'").fetchone()[0] == "queued"
		assert connection.execute("SELECT value_json FROM checkpoints WHERE key='body_parser_version'").fetchone() is None
	with Store(path, ACCOUNT) as store:
		assert not store.get_message("old").body


def test_old_automatic_plan_cannot_pass_preflight_after_parser_upgrade(tmp_path):
	path = tmp_path / "action.sqlite"
	store = Store(path, ACCOUNT)
	box = MemoryMailbox()
	box.account = ACCOUNT
	message = mail()
	box.messages[message.id] = message
	config = Config(dry_run=False, protect_starred=False)
	service = SimpleNamespace(store=store, mailbox=box, config=config, account_id=ACCOUNT,
		reload_config=lambda: None, eligible=eligible)
	executor = ActionExecutor(service)
	store.upsert_message(message)
	aid = executor.plan("job", message, "archive", [], "Old assessment", "automatic", resolve_window(config, ANCHOR))
	old_plan = store.one("SELECT * FROM actions WHERE id=?", (aid,))
	store.execute("DELETE FROM checkpoints WHERE key='body_parser_version'")
	store.close()
	with Store(path, ACCOUNT) as upgraded:
		service.store = upgraded
		assert ActionExecutor(service).preflight(old_plan) is None
		assert upgraded.one("SELECT state FROM actions WHERE id=?", (aid,))["state"] == "stale"
		assert box.calls == []


@pytest.mark.parametrize("state", ["paused", "stopped", "waiting"])
def test_parser_upgrade_preserves_operational_waits_and_user_control_state(tmp_path, state):
	path = tmp_path / "waiting.sqlite"
	due = stamp(ANCHOR + timedelta(hours=1))
	payload = {"pause": {"origin": "user" if state != "waiting" else "system", "due": due},
		"backoff": {"kind": "rate_limit", "due": due, "attempts": 1}}
	with Store(path, ACCOUNT) as store:
		store.upsert_message(mail())
		store.execute("INSERT INTO jobs(id,state,payload_json) VALUES('waiting',?,?)", (state, encode(payload)))
		store.execute("INSERT INTO work_items(job_id,message_id,stage,state,next_attempt_utc) VALUES('waiting','old','classification','retry',?)", (due,))
		store.execute("DELETE FROM checkpoints WHERE key='body_parser_version'")
	with Store(path, ACCOUNT) as store:
		job = store.one("SELECT state,payload_json FROM jobs WHERE id='waiting'")
		assert job["state"] == state and json.loads(job["payload_json"]) == payload
		items = store.query("SELECT stage,state,next_attempt_utc FROM work_items ORDER BY stage")
		assert items == [
			{"stage": "classification", "state": "retry", "next_attempt_utc": due},
			{"stage": "metadata", "state": "retry", "next_attempt_utc": due},
		]
		assert not store.query("SELECT id FROM work_items WHERE next_attempt_utc<=?", (stamp(ANCHOR),))


@pytest.mark.parametrize("resume_existing", [False, True])
def test_maintenance_refetches_original_mime_and_recovers_hidden_content_flags(tmp_path, resume_existing):
	class ReparsingMailbox(RecordingMailbox):
		def get_messages(self, ids, body=False):
			result = super().get_messages(ids, body)
			if body:
				payload = {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(
					b"<p>Visible receipt</p><div hidden>Legacy hidden footer.</div>").decode()}}
				parsed = parse_body(payload)
				result = {mid: replace(value, body=parsed.text, truncated=parsed.truncated,
					missing_content=parsed.missing, content_flags=parsed.content_flags) for mid, value in result.items()}
			return result
	path = tmp_path / "pipeline.sqlite"
	service = PipelineService(path, [])
	service.mailbox = ReparsingMailbox([mail(body="Visible receipt\nLegacy hidden footer.")])
	runner = JobRunner(service)
	window = resolve_window(service.config, ANCHOR)
	service.store.upsert_message(service.mailbox.messages["old"])
	service.store.execute("INSERT INTO predictions(id,message_id,valid,prompt_version) VALUES('old','old',1,?)", (PROMPT_VERSION,))
	for key, value in {
		"history_id": "100", "full_reconciliation": stamp(ANCHOR),
		"boundary_anchor": stamp(ANCHOR), "boundary_latest": window.to_dict()["latest_exclusive"],
		"boundary_window": {name: getattr(service.config, name) for name in ("earliest_date", "latest_mode", "latest_days", "latest_date", "timezone")},
		"boundary_policy_revision": service.config.revision, "boundary_rules_revision": runner.rules_revision(),
		"boundary_prompt_version": PROMPT_VERSION,
	}.items():
		service.store.set_checkpoint(key, value)
	job = runner.new() if resume_existing else None
	if job:
		runner.save_payload(job, {"discovered": True})
		service.store.execute("UPDATE jobs SET state='paused',stage='classification' WHERE id=?", (job,))
		for stage in ("metadata", "body", "classification"):
			runner.queue(job, "old", stage)
		service.store.execute("UPDATE work_items SET state='completed' WHERE stage!='classification'")
	service.store.execute("DELETE FROM checkpoints WHERE key='body_parser_version'")
	service.store.close()
	service.store = Store(path, ACCOUNT)
	try:
		runner = JobRunner(service)
		result = runner.run(job or runner.new())
		assert result["state"] == "completed" and result["failed"] == 0
		assert service.mailbox.body_fetches == ["old"]
		restored = service.store.get_message("old")
		assert restored.body == "Visible receipt"
		assert restored.content_flags == ["hidden_content"]
		assert service.store.one("SELECT decision FROM messages WHERE id='old'")["decision"] == "review"
		assert service.ai.calls == [] and service.actions.planned == []
	finally:
		service.store.close()
