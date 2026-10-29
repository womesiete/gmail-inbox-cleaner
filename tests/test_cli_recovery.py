"""Offline CLI recovery verifies data and identity before opening a service."""
import json
from types import SimpleNamespace

import pytest

from gmail_clean import app
from gmail_clean.runtime import digest
from gmail_clean.service import AppService
from gmail_clean.storage import Store


ACCOUNT = "demo@example.invalid"


def snapshot(path):
	with Store(path, ACCOUNT) as store:
		store.set_checkpoint("recovery-proof", {"retained": True})
	return path


def test_confirmed_cli_restore_verifies_and_exits_before_service_open(tmp_path, monkeypatch):
	source = snapshot(tmp_path / "source.sqlite")
	root = tmp_path / "data"
	monkeypatch.setattr(app, "AppService", lambda **kwargs: pytest.fail("Restore must exit before opening a mailbox service"))
	assert app.main(["--demo", "--headless", "--restore-backup", str(source), "--restore-account", ACCOUNT,
		"--confirm-restore", "--data-dir", str(root)]) == 0
	target = root / "demo" / ("mailbox-" + digest(ACCOUNT)[:12] + ".sqlite")
	with Store(target, ACCOUNT) as restored:
		assert restored.checkpoint("recovery-proof") == {"retained": True}


@pytest.mark.parametrize("extra", [[], ["--restore-account", ACCOUNT], ["--restore-account", "other@example.invalid", "--confirm-restore"]])
def test_cli_restore_rejects_missing_identity_confirmation_or_wrong_account(tmp_path, extra):
	source = snapshot(tmp_path / "source.sqlite")
	root = tmp_path / "data"
	assert app.main(["--demo", "--headless", "--restore-backup", str(source), "--data-dir", str(root), *extra]) == 1
	assert not list((root / "demo").glob("mailbox-*.sqlite"))


def test_queued_restore_request_is_consumed_only_after_verified_restore(tmp_path):
	source = snapshot(tmp_path / "source.sqlite")
	directory = tmp_path / "queued"
	directory.mkdir()
	request = directory / "restore-request.json"
	request.write_text(json.dumps({"source": str(source), "account_id": ACCOUNT}))
	args = SimpleNamespace(restore_backup=None)
	assert app._restore_before_open(args, directory) is False
	assert not request.exists()
	request.write_text(json.dumps({"source": str(tmp_path / "missing.sqlite"), "account_id": ACCOUNT}))
	with pytest.raises(ValueError, match="does not exist"):
		app._restore_before_open(args, directory)
	assert request.exists()


def test_break_lease_without_database_does_not_create_one(tmp_path):
	assert app.break_lease_metadata(tmp_path, demo=True, confirmed=True)["broken"] is False
	assert not list(tmp_path.glob("*.sqlite"))


@pytest.mark.parametrize("extra", [["--apply"], ["--live", "--yes"], ["--resume", "job"], ["--model", "demo-default"]])
def test_break_lease_refuses_combined_execution_commands(tmp_path, extra):
	assert app.main(["--demo", "--headless", "--break-lease", "--confirm-break-lease", "--data-dir", str(tmp_path), *extra]) == 1


def test_cli_apply_in_simulation_is_rejected_without_a_write(tmp_path, monkeypatch):
	monkeypatch.setattr(AppService, "run_job", lambda self, job_id, **kwargs: {"state": "completed", "failed": 0})
	monkeypatch.setattr(AppService, "apply_actions", lambda *args, **kwargs: pytest.fail("Simulation cannot write"))
	assert app.main(["--demo", "--headless", "--apply", "--count", "2", "--data-dir", str(tmp_path)]) == 1


@pytest.mark.parametrize("args", [["--undo-unverified"], ["--undo-unverified", "--undo", "action"], ["--headless", "--live"]])
def test_cli_write_permissions_are_rejected_before_creating_state(tmp_path, args):
	with pytest.raises(SystemExit) as caught:
		app.main(["--data-dir", str(tmp_path / "untouched"), *args])
	assert caught.value.code == 2
	assert not (tmp_path / "untouched").exists()
