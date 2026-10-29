"""Runner/writer separation preserves action receipts across slow AI calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading

import pytest

from gmail_clean.demo import DEMO_ACCOUNT, DemoMailbox
from gmail_clean.domain import Config, Message, context_hash, content_hash, epoch_ms, resolve_window
from gmail_clean.service import AppService
from gmail_clean.storage import LeaseBusyError, LeaseLostError
from test_jobs import RecordingAI

ANCHOR = datetime(2026, 10, 28, 12, tzinfo=UTC)


class BlockingAI(RecordingAI):
	def __init__(self):
		super().__init__()
		self.entered = threading.Event()
		self.release = threading.Event()
		self.score = 100

	def assess(self, *args, **kwargs):
		self.entered.set()
		assert self.release.wait(10), 'Test did not release the synthetic provider'
		return super().assess(*args, **kwargs)


def prepared_service(tmp_path, ai, *, auto_execute=False):
	mailbox = DemoMailbox(count=0, anchor=ANCHOR)
	message = Message('target', 'target', epoch_ms(ANCHOR - timedelta(days=120)), {'INBOX'},
		{'from': 'records@example.invalid', 'subject': 'Completed account record'},
		body='Payment completed. No action required.', missing_content=False, account_id=DEMO_ACCOUNT)
	mailbox.add_message(message)
	config = Config(ai_module='gmail_clean.demo_ai', dry_run=False, protect_starred=False,
		learning_enabled=False, auto_execute=auto_execute)
	service = AppService(mailbox=mailbox, config=config, data_dir=tmp_path, demo=True,
		clock=lambda: ANCHOR, ai_factory=lambda *args, **kwargs: ai)
	message = mailbox.get_message('target', body=True)
	message.context_version = context_hash(mailbox.thread_metadata('target'), False)
	message.content_hash = content_hash(message)
	service.store.upsert_message(message)
	job = service.new_job()
	service.jobs.save_payload(job, {'discovered': True})
	service.jobs.queue(job, 'target', 'classification')
	return service, message, job


def run_worker(service, job):
	with service.worker() as worker:
		return worker.run_job(job)


@pytest.mark.parametrize('undo', [False, True])
def test_apply_and_undo_run_during_classification_without_stale_receipts(tmp_path, undo):
	ai = BlockingAI()
	service, message, job = prepared_service(tmp_path, ai)
	aid = service.actions.plan('', message, 'archive', [], 'Explicit reviewed archive', 'manual', resolve_window(service.config, ANCHOR))
	try:
		with ThreadPoolExecutor(max_workers=1) as pool:
			future = pool.submit(run_worker, service, job)
			try:
				assert ai.entered.wait(5)
				assert service.store.one("SELECT 1 FROM job_leases WHERE name='mailbox_runner'")
				assert not service.store.one("SELECT 1 FROM job_leases WHERE name='mailbox_execution'")
				with service.worker() as competing:
					with pytest.raises(LeaseBusyError, match='runner'):
						competing.run_job(job)
				with service.worker() as writer:
					assert writer.apply_actions([aid])['applied'] == 1
					assert 'INBOX' not in writer.mailbox.get_message('target').labels
					if undo:
						assert writer.undo([aid])['applied'] == 1
						assert 'INBOX' in writer.mailbox.get_message('target').labels
				assert not ai.release.is_set()
			finally:
				ai.release.set()
			assert future.result(timeout=10)['state'] == 'completed'
		assert service.store.one('SELECT state FROM actions WHERE id=?', (aid,))['state'] == ('undone' if undo else 'applied')
		assert not service.store.one("SELECT 1 FROM attempts WHERE state='sending'")
		if not undo:
			# The model's pre-archive result was discarded. Fresh preflight sees
			# the archived target and cannot create a second archive proposal.
			assert not service.store.one('SELECT 1 FROM predictions')
			assert not service.store.one("SELECT 1 FROM actions WHERE state IN ('queued','proposed')")
	finally:
		ai.release.set()
		service.close()


def test_only_one_writer_can_run_even_without_a_runner(tmp_path):
	service, message, _ = prepared_service(tmp_path, RecordingAI())
	aid = service.actions.plan('', message, 'archive', [], 'Explicit archive', 'manual', resolve_window(service.config, ANCHOR))
	entered, release = threading.Event(), threading.Event()
	def hold_writer():
		with service.worker() as writer:
			with writer.execution_lease():
				entered.set()
				assert release.wait(10)
	try:
		with ThreadPoolExecutor(max_workers=1) as pool:
			future = pool.submit(hold_writer)
			try:
				assert entered.wait(5)
				with service.worker() as competing:
					with pytest.raises(LeaseBusyError, match='writer'):
						competing.apply_actions([aid])
				assert 'INBOX' in service.mailbox.get_message('target').labels
				assert not service.store.one('SELECT 1 FROM attempts')
			finally:
				release.set()
			future.result(timeout=10)
	finally:
		release.set()
		service.close()


def test_fenced_runner_cannot_save_provider_result_or_auto_execute(tmp_path):
	ai = BlockingAI()
	service, _, job = prepared_service(tmp_path, ai, auto_execute=True)
	try:
		with ThreadPoolExecutor(max_workers=1) as pool:
			future = pool.submit(run_worker, service, job)
			try:
				assert ai.entered.wait(5)
				lease = service.store.one("SELECT owner_token FROM job_leases WHERE name='mailbox_runner'")
				assert service.store.break_lease('mailbox_runner', confirmed=True, owner_token=lease['owner_token'])
			finally:
				ai.release.set()
			with pytest.raises(LeaseLostError):
				future.result(timeout=10)
		assert 'INBOX' in service.mailbox.get_message('target').labels
		assert not service.store.one('SELECT 1 FROM predictions')
		assert not service.store.one('SELECT 1 FROM attempts')
	finally:
		ai.release.set()
		service.close()


def test_auto_execution_uses_the_single_writer_after_classification(tmp_path):
	service, _, job = prepared_service(tmp_path, RecordingAI(), auto_execute=True)
	try:
		assert service.run_job(job)['state'] == 'completed'
		assert 'INBOX' not in service.mailbox.get_message('target').labels
		assert service.store.one("SELECT state FROM actions WHERE action='archive'")['state'] == 'applied'
		assert not service.store.one('SELECT 1 FROM job_leases')
	finally:
		service.close()


def test_cli_repair_fences_both_observed_runner_and_writer(tmp_path):
	from gmail_clean.app import break_lease_metadata
	service, _, _ = prepared_service(tmp_path, RecordingAI())
	try:
		assert service.store.acquire_lease('mailbox_runner')
		assert service.store.acquire_lease('mailbox_execution')
		result = break_lease_metadata(tmp_path, demo=True, confirmed=True)
		assert result['broken'] and set(result['leases']) == {'mailbox_runner', 'mailbox_execution'}
		assert not service.store.one('SELECT 1 FROM job_leases')
	finally:
		service.close()


def test_gui_apply_and_undo_use_the_independent_pool(qtbot, tmp_path, monkeypatch):
	from gmail_clean.gui import MainWindow
	service = AppService(demo=True, count=0, data_dir=tmp_path)
	window = MainWindow(service)
	qtbot.addWidget(window)
	calls = []
	monkeypatch.setattr(window, '_submit', lambda *args, **kwargs: calls.append(kwargs))
	try:
		window.apply_plan(True)
		window._submit_undo(['reviewed-action'], 'Undo reviewed action')
		assert len(calls) == 2 and all(call['pool'] is window.preview_pool for call in calls)
	finally:
		window.close()
		service.close()


@pytest.mark.parametrize('old_result', ['success', 'provider_error', 'network_error'])
def test_fenced_delayed_runner_cannot_pause_a_successor_running_the_same_job(tmp_path, old_result):
	from gmail_clean.gmail import GmailError
	class SequencedAI(RecordingAI):
		def __init__(self):
			super().__init__()
			self.entered = [threading.Event(), threading.Event()]
			self.release = [threading.Event(), threading.Event()]
			self.lock = threading.Lock()
			self.next_call = 0
		def assess(self, *args, **kwargs):
			with self.lock:
				index = self.next_call
				self.next_call += 1
			self.entered[index].set()
			assert self.release[index].wait(10)
			if index == 0 and old_result == 'provider_error':
				raise RuntimeError('Synthetic provider failed after lease repair')
			if index == 0 and old_result == 'network_error':
				raise GmailError('network', 'Synthetic translated quota callback failure')
			return super().assess(*args, **kwargs)
	ai = SequencedAI()
	service, _, job = prepared_service(tmp_path, ai)
	try:
		with ThreadPoolExecutor(max_workers=2) as pool:
			old = pool.submit(run_worker, service, job)
			try:
				assert ai.entered[0].wait(5)
				lease = service.store.one("SELECT owner_token FROM job_leases WHERE name='mailbox_runner'")
				assert service.store.break_lease('mailbox_runner', confirmed=True, owner_token=lease['owner_token'])
				new = pool.submit(run_worker, service, job)
				assert ai.entered[1].wait(5)
				before = service.store.one('SELECT state,payload_json FROM jobs WHERE id=?', (job,))
				work_before = service.store.one("SELECT state,attempts,payload_json FROM work_items WHERE job_id=? AND stage='classification'", (job,))
				ai.release[0].set()
				with pytest.raises(LeaseLostError):
					old.result(timeout=5)
				assert service.store.one('SELECT state,payload_json FROM jobs WHERE id=?', (job,)) == before
				assert service.store.one("SELECT state,attempts,payload_json FROM work_items WHERE job_id=? AND stage='classification'", (job,)) == work_before
				assert before['state'] == 'running' and work_before['state'] == 'running'
				ai.release[1].set()
				assert new.result(timeout=5)['state'] == 'completed'
			finally:
				for event in ai.release:
					event.set()
	finally:
		for event in ai.release:
			event.set()
		service.close()


def test_gui_repair_runs_when_both_runner_and_writer_pools_are_blocked(qtbot, tmp_path, monkeypatch):
	from gmail_clean.gui import MainWindow
	from PyQt6.QtWidgets import QMessageBox
	service = AppService(demo=True, count=0, data_dir=tmp_path)
	window = MainWindow(service)
	qtbot.addWidget(window)
	writer_entered, runner_entered, release = threading.Event(), threading.Event(), threading.Event()
	def writer(worker, progress):
		with worker.execution_lease():
			writer_entered.set()
			assert release.wait(10)
	def runner(worker, progress):
		with worker.runner_lease():
			runner_entered.set()
			assert release.wait(10)
	window._submit('Blocked cleanup runner', runner)
	window._submit('Blocked mailbox writer', writer, pool=window.preview_pool)
	try:
		qtbot.waitUntil(lambda: writer_entered.is_set() and runner_entered.is_set(), timeout=5000)
		monkeypatch.setattr(QMessageBox, 'warning', lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
		window._break_lease()
		qtbot.waitUntil(lambda: not service.store.one("SELECT 1 FROM job_leases WHERE name IN ('mailbox_execution','mailbox_runner')"), timeout=5000)
		assert not release.is_set()
	finally:
		release.set()
		qtbot.waitUntil(lambda: not window.tasks, timeout=5000)
		window.close()
		service.close()
