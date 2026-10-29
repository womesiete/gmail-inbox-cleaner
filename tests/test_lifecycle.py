"""Integrated retry, worker-resource, and standalone shutdown regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from gmail_clean.ai import AIClient
from gmail_clean.app import run_until_settled
from gmail_clean.domain import Config
from gmail_clean.gmail import GmailError
from gmail_clean.service import AppService


class Clock:
	def __init__(self):
		self.now = datetime(2026, 10, 28, 12, tzinfo=UTC)
		self.waits = []

	def __call__(self):
		return self.now

	def sleep(self, delay):
		self.waits.append(delay)
		self.now += timedelta(seconds=delay)


def test_headless_resumes_due_transient_retries_with_frozen_job_scope(tmp_path):
	clock = Clock()
	service = AppService(demo=True, count=8, data_dir=tmp_path, clock=clock, config=Config(max_retries=1))
	try:
		service.mailbox.inject_error("get_messages", GmailError("rate_limit", "Synthetic retry", retry_after=75))
		job = service.new_job()
		anchor = service.store.one("SELECT anchor_utc FROM jobs WHERE id=?", (job,))["anchor_utc"]
		result = run_until_settled(service, job, sleep=clock.sleep)
		assert result["state"] == "completed" and result["failed"] == 0
		assert clock.waits and max(clock.waits) <= 30
		assert sum(clock.waits) >= 75
		assert service.store.one("SELECT anchor_utc FROM jobs WHERE id=?", (job,))["anchor_utc"] == anchor
		assert not service.store.one("SELECT id FROM work_items WHERE job_id=? AND state='retry'", (job,))
	finally:
		service.close()


def test_transport_recovery_and_configuration_pause_do_not_loop_forever(tmp_path):
	clock = Clock()
	service = AppService(demo=True, count=4, data_dir=tmp_path / "transient", clock=clock, config=Config(max_retries=1))
	try:
		for _ in range(2):
			service.mailbox.inject_error("get_messages", GmailError("network", "Synthetic offline retry"))
		result = run_until_settled(service, service.new_job(), sleep=clock.sleep)
		assert result["state"] == "completed" and result["failed"] == 0
		assert clock.waits
	finally:
		service.close()
	clock = Clock()
	service = AppService(demo=True, count=4, data_dir=tmp_path / "configuration", clock=clock, config=Config(ai_model="demo-missing-key"))
	try:
		result = run_until_settled(service, service.new_job(), sleep=clock.sleep)
		assert result["state"] == "paused" and clock.waits == []
	finally:
		service.close()


def test_headless_wait_honors_an_explicit_pause_before_retry_is_due(tmp_path):
	clock = Clock()
	service = AppService(demo=True, count=4, data_dir=tmp_path, clock=clock)
	try:
		service.mailbox.inject_error("get_messages", GmailError("network", "Synthetic retry"))
		job = service.new_job()
		def pause_during_wait(delay):
			clock.sleep(delay)
			service.control_job(job, "paused")
		result = run_until_settled(service, job, sleep=pause_during_wait)
		assert result["state"] == "paused"
		assert service.store.one("SELECT COUNT(*) n FROM work_items WHERE job_id=? AND state='retry'", (job,))["n"] > 0
		assert service.mailbox.body_reads == []
	finally:
		service.close()


def test_fresh_worker_stores_reuse_one_isolated_ai_module_process(tmp_path):
	module = tmp_path / "counting_module.py"
	module.write_text("import os\ncalls=0\ndef call_api(prompt):\n\tglobal calls\n\tcalls+=1\n\treturn str(calls)+':'+str(os.getpid())\n", encoding="utf-8")
	def factory(location, **kwargs):
		return AIClient(str(module), **kwargs)
	service = AppService(demo=True, count=4, data_dir=tmp_path / "data", ai_factory=factory)
	try:
		def task():
			with service.worker() as worker:
				assert worker.store is not service.store
				return worker.ai.call("count").text
		with ThreadPoolExecutor(max_workers=1) as pool:
			first = pool.submit(task).result(timeout=30)
			second = pool.submit(task).result(timeout=30)
		assert first.split(":")[0] == "1"
		assert second.split(":")[0] == "2"
		assert first.split(":")[1] == second.split(":")[1] != str(os.getpid())
		assert service.active_worker_count() == 0
		assert len(service._clients) == 1
	finally:
		service.close()
	assert service._clients == {}
	with pytest.raises(RuntimeError, match="shutdown"):
		with service.worker():
			pytest.fail("A closed application must not start another worker")


def test_cancelled_service_commands_do_not_start_forward_mailbox_work(tmp_path):
	service = AppService(demo=True, count=8, data_dir=tmp_path)
	try:
		job = service.new_job()
		service.run_job(job)
		ids = [row["id"] for row in service.store.query("SELECT id FROM messages WHERE in_inbox=1")]
		service.mailbox.inject_error("get", AssertionError("cancelled manual command must not fetch"))
		assert service.manual(ids, "archive", should_stop=lambda: True) == []
		assert service.replan(should_stop=lambda: True)["replanned"] == 0
		assert service.download_bodies(ids, should_stop=lambda: True)["downloaded"] == 0
		assert service.store.one("SELECT COUNT(*) n FROM feedback")["n"] == 0
		assert service.mailbox.mutations == []
	finally:
		service.close()


def test_identity_commit_uses_unique_temporaries_and_cleans_failed_replace(tmp_path, monkeypatch):
	service = AppService(demo=True, count=4, data_dir=tmp_path)
	try:
		with ThreadPoolExecutor(max_workers=4) as pool:
			list(pool.map(lambda _: service._write_identity(), range(8)))
		previous = json.loads((tmp_path / "account.json").read_text())
		assert previous["account_id"] == service.account_id
		assert not list(tmp_path.glob("account.json.*.tmp"))
		original = Path.replace
		def failed_replace(path, target):
			if path.name.startswith("account.json."):
				raise OSError("synthetic atomic commit failure")
			return original(path, target)
		monkeypatch.setattr(Path, "replace", failed_replace)
		with pytest.raises(OSError):
			service._write_identity()
		assert json.loads((tmp_path / "account.json").read_text()) == previous
		assert not list(tmp_path.glob("account.json.*.tmp"))
	finally:
		service.close()


def test_identity_readers_and_replacers_share_a_guard_across_service_instances(tmp_path, monkeypatch):
	first = AppService(demo=True, count=4, data_dir=tmp_path)
	second = AppService(demo=True, count=4, data_dir=tmp_path)
	first._write_identity()
	target = tmp_path / "account.json"
	read_text, replace = Path.read_text, Path.replace
	guard = threading.Lock()
	busy = [False]
	def exclusive(operation):
		with guard:
			if busy[0]:
				raise PermissionError("Synthetic Windows destination is open or being replaced")
			busy[0] = True
		try:
			time.sleep(0.005)
			return operation()
		finally:
			with guard:
				busy[0] = False
	def checked_read(path, *args, **kwargs):
		return exclusive(lambda: read_text(path, *args, **kwargs)) if path == target else read_text(path, *args, **kwargs)
	def checked_replace(path, destination):
		return exclusive(lambda: replace(path, destination)) if destination == target else replace(path, destination)
	monkeypatch.setattr(Path, "read_text", checked_read)
	monkeypatch.setattr(Path, "replace", checked_replace)
	try:
		def operation(index):
			service = first if index % 2 else second
			if index % 3:
				service._write_identity()
			else:
				assert service._connection_state()["account_id"] == first.account_id
		with ThreadPoolExecutor(max_workers=4) as pool:
			list(pool.map(operation, range(24)))
		assert first._connection_state()["account_id"] == first.account_id
		assert not list(tmp_path.glob("account.json.*.tmp"))
	finally:
		first.close()
		second.close()


def test_standalone_close_deadline_does_not_wait_for_a_blocked_network_thread(tmp_path):
	# The application entry point may end its own process after the explicit
	# close deadline. Embedders/tests closing a MainWindow never exit their host.
	script = """import sys,time
from PyQt6.QtCore import QTimer
from gmail_clean import app,gui
Original=gui.MainWindow
class BlockedWindow(Original):
	def __init__(self,service,settings=None):
		super().__init__(service,settings=settings)
		self._submit('synthetic blocked network',lambda svc,p:time.sleep(60))
		QTimer.singleShot(200,self.close)
gui.MainWindow=BlockedWindow
sys.argv=['gmail-clean','--demo','--count','4','--data-dir',sys.argv[1]]
raise SystemExit(app.main())
"""
	result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True,
		text=True, timeout=30, check=False, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
	assert result.returncode == 0, result.stderr
	notice = tmp_path / "demo" / "shutdown-notice.log"
	assert "unfinished network work" in notice.read_text(encoding="utf-8")


def test_headless_recovers_system_discovery_pause_with_bounded_backoff(tmp_path):
	clock = Clock()
	service = AppService(demo=True, count=4, data_dir=tmp_path, clock=clock, config=Config(max_retries=2))
	try:
		service.mailbox.inject_error("list", GmailError("network", "Synthetic discovery interruption"))
		job = service.new_job()
		result = run_until_settled(service, job, sleep=clock.sleep)
		assert result["state"] == "completed"
		assert clock.waits and sum(clock.waits) < 300
	finally:
		service.close()


def test_headless_discovery_outage_recovers_beyond_the_item_retry_budget(tmp_path):
	clock = Clock()
	service = AppService(demo=True, count=4, data_dir=tmp_path, clock=clock, config=Config(max_retries=1))
	try:
		for _ in range(4):
			service.mailbox.inject_error("list", GmailError("network", "Synthetic persistent interruption"))
		job = service.new_job()
		result = run_until_settled(service, job, sleep=clock.sleep)
		assert result["state"] == "completed" and result["failed"] == 0
		assert "pause" not in service.jobs.payload(job)
		assert clock.waits and sum(clock.waits) < 300
	finally:
		service.close()


def test_user_pause_origin_is_persisted_and_never_auto_resumed(tmp_path):
	service = AppService(demo=True, count=4, data_dir=tmp_path)
	try:
		job = service.new_job()
		service.control_job(job, "paused")
		assert service.jobs.payload(job)["pause"]["origin"] == "user"
	finally:
		service.close()
