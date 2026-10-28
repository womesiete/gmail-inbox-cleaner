"""Kill a separate offline process at durable write boundaries, then reopen it."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys

import pytest

from gmail_clean.domain import Config, Message, context_hash, epoch_ms, resolve_window
from gmail_clean.service import AppService


_CRASH_PROCESS = r'''
import os
import sys
from gmail_clean import actions as ledger
from gmail_clean.service import AppService

service = AppService(data_dir=sys.argv[1], demo=True, count=0)
aid, boundary = sys.argv[2:]
if boundary == "before_attempt":
	original = ledger.insert
	def insert(store, table, values, **kwargs):
		if table == "attempts":
			os._exit(91)
		return original(store, table, values, **kwargs)
	ledger.insert = insert
elif boundary == "after_modify":
	original = service.mailbox.modify
	def modify(*args, **kwargs):
		original(*args, **kwargs)
		os._exit(91)
	service.mailbox.modify = modify
elif boundary == "before_receipt":
	def receipt(*args, **kwargs):
		os._exit(91)
	service.actions.receipt = receipt
service.apply_actions([aid])
raise RuntimeError("Crash hook was not reached")
'''


@pytest.mark.parametrize("boundary", ["before_attempt", "after_modify", "before_receipt"])
def test_process_crash_recovers_without_duplicate_mailbox_writes(tmp_path, boundary):
	anchor = datetime(2026, 10, 28, 15, 30, tzinfo=UTC)
	service = AppService(data_dir=tmp_path, demo=True, count=0, config=Config(dry_run=False), clock=lambda: anchor)
	message = Message("crash-target", "thread-crash", epoch_ms(anchor - timedelta(days=100)),
		labels={"INBOX", "UNREAD"}, headers={"From": "offline@example.invalid", "Subject": "Synthetic completed record"},
		body="Offline synthetic record.", missing_content=False, account_id=service.account_id)
	service.mailbox.add_message(message)
	message.context_version = context_hash(service.mailbox.thread_metadata(message.thread_id), False)
	service.store.upsert_message(message)
	aid = service.actions.plan("", message, "archive", [], "Explicit synthetic test", "manual", resolve_window(service.config, anchor))
	history_before = int(service.mailbox.profile()["historyId"])
	service.close()

	environment = dict(os.environ)
	environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
	crashed = subprocess.run([sys.executable, "-c", _CRASH_PROCESS, str(tmp_path), aid, boundary],
		env=environment, capture_output=True, text=True, timeout=30)
	assert crashed.returncode == 91, crashed.stderr

	service = AppService(data_dir=tmp_path, demo=True, count=0, clock=lambda: anchor)
	try:
		before_recovery = service.store.one("SELECT state FROM actions WHERE id=?", (aid,))["state"]
		assert before_recovery == ("queued" if boundary == "before_attempt" else "executing")
		result = service.apply_actions([aid])
		row = service.store.one("SELECT state,payload_json FROM actions WHERE id=?", (aid,))
		assert row["state"] == ("applied" if boundary == "before_attempt" else "reconciled")
		assert result["applied"] == (1 if boundary == "before_attempt" else 0)
		assert "INBOX" not in service.mailbox.get_message(message.id).labels
		assert int(service.mailbox.profile()["historyId"]) == history_before + 1
		assert service.store.one("SELECT count(*) n FROM attempts WHERE action_id=?", (aid,))["n"] == 1
		# Reopening and reconciling again never makes the archive call twice.
		assert service.apply_actions([aid])["applied"] == 0
		assert int(service.mailbox.profile()["historyId"]) == history_before + 1
	finally:
		service.close()
