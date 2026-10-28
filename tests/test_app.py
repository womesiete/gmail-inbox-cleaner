"""Console execution needs its own write opt-in, independent of desktop prefs."""

from gmail_clean.app import main
from gmail_clean.domain import Config
from gmail_clean.service import AppService


def test_headless_defaults_to_simulation_even_with_saved_live_automation(tmp_path):
	root = tmp_path / "data"
	service = AppService(data_dir=root / "demo", demo=True, count=30, config=Config(dry_run=False, auto_execute=True))
	before = {mid: set(m.labels) for mid, m in service.mailbox.messages.items()}
	service.close()
	assert main(["--demo", "--headless", "--count", "30", "--data-dir", str(root)]) == 0
	service = AppService(data_dir=root / "demo", demo=True)
	try:
		assert {mid: set(m.labels) for mid, m in service.mailbox.messages.items()} == before
		assert not service.store.one("SELECT id FROM actions WHERE state IN ('applied','reconciled')")
		assert service.config.dry_run is False  # Desktop preference was never changed.
	finally:
		service.close()


def test_explicit_console_live_apply_restores_desktop_simulation(tmp_path):
	root = tmp_path / "data"
	assert main(["--demo", "--headless", "--count", "30", "--live", "--yes", "--apply", "--data-dir", str(root)]) == 0
	service = AppService(data_dir=root / "demo", demo=True)
	try:
		assert service.config.dry_run is True
		assert service.store.one("SELECT id FROM actions WHERE state='applied'")
	finally:
		service.close()


def test_bulk_manual_network_preflight_does_not_hold_sqlite_writer_lock(tmp_path):
	service = AppService(data_dir=tmp_path, demo=True, count=30)
	try:
		job = service.new_job()
		service.run_job(job)
		ids = [row["id"] for row in service.store.query("SELECT id FROM messages WHERE decision IN ('archive','review','keep') LIMIT 3")]
		original_get = service.mailbox.get_message
		def checked_get(*args, **kwargs):
			assert not service.store.conn.in_transaction
			return original_get(*args, **kwargs)
		service.mailbox.get_message = checked_get
		service.manual(ids, "keep")
		assert service.store.one("SELECT COUNT(*) n FROM manual_overrides")["n"] == len(ids)
	finally:
		service.close()


def test_headless_live_requires_explicit_bound_or_confirmation(tmp_path):
	import pytest
	with pytest.raises(SystemExit) as caught:
		main(["--demo", "--headless", "--live", "--count", "2", "--data-dir", str(tmp_path)])
	assert caught.value.code == 2


def test_cli_live_override_never_changes_saved_preferences(tmp_path, monkeypatch):
	root = tmp_path / "data"
	service = AppService(data_dir=root / "demo", demo=True, count=2)
	service.close()
	observed = []
	def interrupted_run(self, job_id, **kwargs):
		observed.append((self.config.dry_run, self.store.checkpoint("effective_preferences")["dry_run"]))
		raise KeyboardInterrupt
	monkeypatch.setattr(AppService, "run_job", interrupted_run)
	assert main(["--demo", "--headless", "--live", "--yes", "--count", "2", "--data-dir", str(root)]) == 130
	assert observed == [(False, True)]
