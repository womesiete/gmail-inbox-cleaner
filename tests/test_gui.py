"""Offline GUI behavior, SQL paging, safe preview, and worker ownership."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings, Qt
import pytest

from gmail_clean.domain import Config, Message
from gmail_clean.gui import (LabelPicker, MainWindow, RuleDialog, SafePreview, SettingsEditor,
	acquire_instance_lock, load_preferences, sanitize_preview_html, save_preferences)
from gmail_clean.storage import Store
from gmail_clean.table_model import Column, SqlTableModel


class FakeService:
	def __init__(self, path, shared=None):
		self.config = Config()
		self.store = Store(path, "demo@example.invalid")
		self.account_id = "demo@example.invalid"
		self.demo = True
		self.data_dir = self.store.path.parent
		self.shared = shared if shared is not None else []

	@contextmanager
	def worker(self):
		child = FakeService(self.store.path, self.shared)
		child.config = self.config
		try:
			yield child
		finally:
			child.close()

	def close(self):
		self.store.close()

	def manual(self, ids, action, **kwargs):
		self.shared.append((threading.get_ident(), list(ids), action, kwargs))
		self.store.execute("UPDATE messages SET decision=? WHERE id=?", (action, ids[0]))
		return ["review-action"]

	def control_job(self, job_id, state):
		self.store.execute("UPDATE jobs SET state=? WHERE id=?", (state, job_id))

	def refresh_connection(self):
		return False


@pytest.fixture
def service(tmp_path):
	return FakeService(tmp_path / "demo.sqlite")


def add_message(service, identifier="old", *, age_days=90, sender="Clinic <clinic@example.invalid>"):
	message = Message(id=identifier, thread_id=f"thread-{identifier}",
		internal_ms=int((datetime.now(timezone.utc) - timedelta(days=age_days)).timestamp() * 1000),
		labels={"INBOX", "UNREAD"}, headers={"From": sender, "Subject": "Review sample"},
		body="A completed receipt.", missing_content=False, account_id=service.account_id)
	service.store.upsert_message(message)
	return message


def test_sql_model_50000_rows_keeps_bounded_cache_and_sql_filter(qapp, tmp_path):
	store = Store(tmp_path / "scale.sqlite", "demo@example.invalid")
	with store.transaction():
		store.conn.executemany("INSERT INTO messages(id,subject,sender,internal_ms) VALUES(?,?,?,?)",
			((f"m{i:05}", f"Message {i:05}", f"sender{i % 20}@example.invalid", i) for i in range(50_000)))
	start = time.monotonic()
	model = SqlTableModel(store.path, "SELECT id,subject,sender,internal_ms FROM messages",
		[Column("subject", "Subject"), Column("internal_ms", "Date")], page_size=100, max_pages=3)
	assert model.rowCount() == 50_000
	for row in (0, 5_000, 10_000, 25_000, 49_999):
		assert model.record(row)["id"] == f"m{row:05}"
	assert len(model._pages) <= 3
	assert sum(map(len, model._pages.values())) <= 300
	model.set_filter("sender=?", ("sender1@example.invalid",))
	assert model.rowCount() == 2500
	model.sort(1, Qt.SortOrder.DescendingOrder)
	assert model.record(0)["internal_ms"] == 49_981
	assert model.rows_for_ids(["m00001"])[0] == 2499
	print(f"SQL model: 50,000 rows, five page reads plus filter/sort in {time.monotonic() - start:.3f}s")
	model.close()
	store.close()


def test_settings_roundtrip_preserves_default_model_omission_and_dates(qapp, tmp_path):
	settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat)
	config = Config(latest_days=0, ai_model="", earliest_date=None)
	save_preferences(config, settings)
	loaded = load_preferences(settings)
	assert loaded.latest_days == 0
	assert loaded.ai_model == ""
	assert loaded.earliest_date is None
	editor = SettingsEditor(loaded)
	assert "unlimited" in editor.resolved.text()
	assert editor.read_config().latest_days == 0
	editor.deleteLater()


def test_preview_strips_scripts_trackers_and_blocks_resources(qapp, qtbot):
	html = '<p onclick="steal()">Hello<script>secret()</script><img src="https://tracker.invalid/x"><a href="javascript:bad()">Click</a></p>'
	clean = sanitize_preview_html(html)
	assert "script" not in clean
	assert "tracker" not in clean
	assert "onclick" not in clean
	assert "secret()" not in clean
	assert "javascript:" not in clean
	preview = SafePreview()
	qtbot.addWidget(preview)
	preview.set_message(html, html=True)
	assert "Hello" in preview.toPlainText()
	from PyQt6.QtCore import QUrl
	assert preview.loadResource(2, QUrl("https://tracker.invalid/x")).isEmpty()
	assert not preview.openExternalLinks()


def test_label_picker_uses_existing_user_ids_and_multiple_additions(qapp, qtbot):
	picker = LabelPicker([
		{"id": "INBOX", "name": "Inbox", "type": "system"},
		{"id": "Label_1", "name": "Receipts", "type": "user"},
		{"id": "Label_2", "name": "Finance", "type": "user"}], selected=["Label_1", "Label_2"], current=["Label_1"])
	qtbot.addWidget(picker)
	assert picker.list.count() == 2
	assert set(picker.chosen_ids()) == {"Label_1", "Label_2"}
	picker.search.setText("receipts")
	assert sum(not picker.list.item(i).isHidden() for i in range(2)) == 1
	assert set(picker.chosen_ids()) == {"Label_1", "Label_2"}


def test_review_filters_excluded_recent_rows_and_runs_manual_off_gui_thread(qapp, qtbot, service, tmp_path):
	add_message(service, "old")
	add_message(service, "recent", age_days=1)
	settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
	window = MainWindow(service, settings)
	qtbot.addWidget(window)
	window.show()
	window.tabs.setCurrentIndex(1)
	assert all(not model.last_error for model in window.models.values()), {k: v.last_error for k, v in window.models.items()}
	assert window.models["review"].rowCount() == 1
	view = window.views["review"]
	view.selectRow(0)
	assert window.models["review"].record(0)["id"] == "old"
	main_thread = threading.get_ident()
	window.review_action("archive")
	qtbot.waitUntil(lambda: not window.tasks, timeout=5000)
	assert service.shared[0][0] != main_thread
	assert service.shared[0][1] == ["old"]
	assert service.shared[0][2] == "archive"
	assert service.shared[0][3]["displayed_revision"] == service.config.revision
	assert service.shared[0][3]["displayed_protections"] == {"old": []}
	assert "old" in service.shared[0][3]["displayed_context_versions"]
	assert window._last_action_ids == ["review-action"]
	window.close()


def test_shortcut_does_not_intercept_typing(qapp, qtbot, service, tmp_path):
	add_message(service)
	window = MainWindow(service, QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	window.show()
	window.tabs.setCurrentIndex(1)
	window.views["review"].selectRow(0)
	window.review_search.setFocus()
	qtbot.keyClicks(window.review_search, "archive")
	assert window.review_search.text() == "archive"
	assert not service.shared
	window.close()


def test_second_instance_lock_rejected_and_release_recovers(qapp, tmp_path):
	first = acquire_instance_lock(tmp_path)
	with pytest.raises(RuntimeError):
		acquire_instance_lock(tmp_path)
	first.unlock()
	second = acquire_instance_lock(tmp_path)
	second.unlock()


def test_real_offline_service_job_finishes_through_gui_worker(qapp, qtbot, tmp_path):
	from gmail_clean.service import AppService
	service = AppService(data_dir=tmp_path / "real-demo", demo=True, count=40)
	window = MainWindow(service, QSettings(str(tmp_path / "real-ui.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	window.show()
	window.start_job()
	qtbot.waitUntil(lambda: not window.tasks, timeout=20000)
	jobs = service.store.query("SELECT state FROM jobs")
	assert jobs and jobs[0]["state"] == "completed"
	assert window.models["review"].rowCount() > 0
	assert window.current_job_id is not None
	assert not window.models["review"].last_error
	assert "SIMULATION" in window.banner.text()
	window.close()


def test_history_previews_out_of_current_window_original_ids(qapp, qtbot, service, tmp_path):
	add_message(service, "old-record", age_days=90)
	service.config.earliest_date = (datetime.now(timezone.utc) - timedelta(days=60)).date().isoformat()
	service.store.execute("INSERT INTO actions(id,message_id,state,action) VALUES('receipt','old-record','executed','archive')")
	window = MainWindow(service, QSettings(str(tmp_path / "undo-ui.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	assert window.models["review"].rowCount() == 0
	assert window.models["history"].rowCount() == 1
	window.views["history"].selectRow(0)
	assert "1 outside the current cleanup date window" in window.undo_scope.text()
	assert "old-record" in window.undo_scope.text()
	assert not window.settings_editor.controls["ai_module"][0].isEnabled()
	window.close()


def test_retry_refresh_resumes_only_due_waiting_job_and_ignores_selected_paused_job(qapp, qtbot, service, tmp_path, monkeypatch):
	window = MainWindow(service, QSettings(str(tmp_path / "retry.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	for job_id, state in (("paused", "paused"), ("future", "waiting"), ("ready", "waiting")):
		service.store.execute("INSERT INTO jobs(id,state) VALUES(?,?)", (job_id, state))
		service.store.execute("INSERT INTO work_items(job_id,message_id,stage,state,next_attempt_utc) VALUES(?,?,'body','retry',?)",
			(job_id, "message-" + job_id, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))
	window.current_job_id = "paused"
	resumed = []
	monkeypatch.setattr(window, "resume_job", lambda job_id=None: resumed.append(job_id))
	window.refresh(force=True)
	assert not resumed
	service.store.execute("UPDATE work_items SET next_attempt_utc=? WHERE job_id IN ('ready','paused')",
		((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
	window.refresh(force=True)
	assert resumed == ["ready"]
	assert window.current_job_id == "ready"
	window.close()


def test_maintenance_resumes_due_retry_before_starting_new_job(qapp, qtbot, service, tmp_path, monkeypatch):
	window = MainWindow(service, QSettings(str(tmp_path / "maintenance-retry.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	service.store.execute("INSERT INTO jobs(id,state) VALUES('waiting','waiting')")
	service.store.execute("INSERT INTO work_items(job_id,message_id,stage,state,next_attempt_utc) VALUES('waiting','m','classification','retry',NULL)")
	resumed = []
	monkeypatch.setattr(window, "resume_job", lambda job_id=None: resumed.append(job_id))
	monkeypatch.setattr(window, "start_job", lambda: pytest.fail("Due retry should resume its frozen job"))
	window._maintenance_tick()
	assert resumed == ["waiting"]
	window.close()


def test_pause_stop_controls_do_not_write_sqlite_on_gui_thread_during_worker_transaction(qapp, qtbot, service, tmp_path, monkeypatch):
	service.store.execute("INSERT INTO jobs(id,state,anchor_utc) VALUES('job','running',?)", (datetime.now(timezone.utc).isoformat(),))
	window = MainWindow(service, QSettings(str(tmp_path / "control.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	locked, release = threading.Event(), threading.Event()
	def long_transaction(worker, progress):
		with worker.store.transaction():
			worker.store.execute("UPDATE jobs SET stage='metadata' WHERE id='job'")
			locked.set()
			release.wait(10)
	window._submit("Transaction", long_transaction)
	qtbot.waitUntil(locked.is_set, timeout=5000)
	monkeypatch.setattr(service, "control_job", lambda *args: pytest.fail("GUI thread attempted a contested database write"))
	try:
		started = time.monotonic()
		window._control("stopped")
		assert time.monotonic() - started < 1
		assert window._requested_control_state == "stopped"
		assert window.stop_event.is_set()
	finally:
		release.set()
		qtbot.waitUntil(lambda: not window.tasks, timeout=5000)
		window.close()


def test_rule_preview_is_not_queued_behind_long_cleanup_operation(qapp, qtbot, service, tmp_path):
	add_message(service)
	window = MainWindow(service, QSettings(str(tmp_path / "preview-pool.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	started, release = threading.Event(), threading.Event()
	def long_job(worker, progress):
		started.set()
		release.wait(10)
	window._submit("Long cleanup", long_job)
	qtbot.waitUntil(started.is_set, timeout=5000)
	dialog = RuleDialog(parent=window)
	try:
		window._preview_rule({"sender": "clinic@example.invalid"}, dialog)
		qtbot.waitUntil(lambda: "1 current eligible matches" in dialog.summary.text(), timeout=5000)
		assert not release.is_set()
		assert window.pool.activeThreadCount() == 1
	finally:
		release.set()
		qtbot.waitUntil(lambda: not window.tasks, timeout=5000)
		dialog.close()
		window.close()


def test_close_fallback_detaches_worker_resources_without_blocking_or_cross_thread_close(qapp, qtbot, service, tmp_path):
	from gmail_clean.gui import _DRAINING_WORK
	window = MainWindow(service, QSettings(str(tmp_path / "close.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	window.show()
	started, release = threading.Event(), threading.Event()
	def blocked_network(worker, progress):
		started.set()
		release.wait(12)
	window._submit("Blocked network", blocked_network)
	qtbot.waitUntil(started.is_set, timeout=5000)
	try:
		before = time.monotonic()
		window.close()
		assert window._closing and window.stop_event.is_set()
		qtbot.waitUntil(lambda: window._resources_closed and not window.isVisible(), timeout=6000)
		assert time.monotonic() - before < 5.5
		assert window.shutdown_incomplete
		assert _DRAINING_WORK
	finally:
		release.set()
		qtbot.waitUntil(lambda: not _DRAINING_WORK, timeout=5000)


def test_close_aborts_active_ai_child_without_waiting_for_provider_timeout(qapp, qtbot, tmp_path):
	from gmail_clean.ai import AIClient
	from gmail_clean.service import AppService
	module = tmp_path / "offline_delayed.py"
	module.write_text("from pathlib import Path\nimport time\ndef call_api(prompt):\n\tPath(__file__).with_suffix('.running').write_text('running')\n\ttime.sleep(30)\n\treturn 'late'\n", encoding="utf-8")
	# This injected module is a strictly offline test provider; production demo
	# still rejects external module configuration and cannot access Gmail.
	service = AppService(data_dir=tmp_path / "ai-close-demo", demo=True, count=10,
		ai_factory=lambda location, **kwargs: AIClient(str(module), **kwargs))
	window = MainWindow(service, QSettings(str(tmp_path / "ai-close.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	window.show()
	window._submit("Delayed synthetic AI call", lambda worker, p: worker.ai.call("synthetic timeout test"))
	qtbot.waitUntil(module.with_suffix(".running").exists, timeout=15000)
	started = time.monotonic()
	window.close()
	qtbot.waitUntil(lambda: window._resources_closed, timeout=3000)
	assert time.monotonic() - started < 3
	assert not window.shutdown_incomplete
	assert service.active_worker_count() == 0


def test_manual_review_after_idle_stop_uses_fresh_cancellation_token(qapp, qtbot, tmp_path):
	from gmail_clean.domain import context_hash
	from gmail_clean.service import AppService
	service = AppService(data_dir=tmp_path / "after-stop-demo", demo=True, count=10)
	message = Message("after-stop", "thread-after-stop",
		int((datetime.now(timezone.utc) - timedelta(days=90)).timestamp() * 1000),
		labels={"INBOX"}, headers={"From": "clinic@example.invalid", "Subject": "Completed record"},
		body="Completed record.", account_id=service.account_id)
	service.mailbox.add_message(message)
	message.context_version = context_hash([message])
	service.store.upsert_message(message)
	job_id = service.new_job()
	window = MainWindow(service, QSettings(str(tmp_path / "after-stop.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	window._control("stopped")
	qtbot.waitUntil(lambda: not window.tasks, timeout=5000)
	assert service.store.one("SELECT state FROM jobs WHERE id=?", (job_id,))["state"] == "stopped"
	assert not window.stop_event.is_set()
	assert window._requested_control_state is None
	window.views["review"].selectRow(0)
	window.review_action("keep")
	qtbot.waitUntil(lambda: not window.tasks, timeout=5000)
	assert service.store.one("SELECT action FROM manual_overrides WHERE message_id='after-stop'")["action"] == "keep"
	assert service.store.one("SELECT COUNT(*) n FROM feedback WHERE message_id='after-stop'")["n"] == 1
	window.close()


def test_pause_waiting_job_is_persisted_while_only_preview_worker_is_active(qapp, qtbot, service, tmp_path, monkeypatch):
	window = MainWindow(service, QSettings(str(tmp_path / "preview-pause.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	service.store.execute("INSERT INTO jobs(id,state,anchor_utc) VALUES('waiting','waiting',?)", (datetime.now(timezone.utc).isoformat(),))
	service.store.execute("INSERT INTO work_items(job_id,message_id,stage,state,next_attempt_utc) VALUES('waiting','m','body','retry',NULL)")
	window.current_job_id = "waiting"
	started, release = threading.Event(), threading.Event()
	def blocked_preview(worker, progress):
		started.set()
		release.wait(10)
	window._submit("Preview", blocked_preview, pool=window.preview_pool)
	qtbot.waitUntil(started.is_set, timeout=5000)
	resumed = []
	monkeypatch.setattr(window, "resume_job", lambda job_id=None: resumed.append(job_id))
	try:
		window._control("paused")
		qtbot.waitUntil(lambda: service.store.one("SELECT state FROM jobs WHERE id='waiting'")["state"] == "paused", timeout=5000)
		assert not release.is_set()
		assert not resumed
	finally:
		release.set()
		qtbot.waitUntil(lambda: not window.tasks, timeout=5000)
	window.refresh(force=True)
	assert not resumed
	assert service.store.one("SELECT state FROM jobs WHERE id='waiting'")["state"] == "paused"
	window.close()


def test_finished_job_clears_running_flag_while_preview_remains_active(qapp, qtbot, service, tmp_path):
	window = MainWindow(service, QSettings(str(tmp_path / "job-with-preview.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	job_started, preview_started = threading.Event(), threading.Event()
	release_job, release_preview = threading.Event(), threading.Event()
	def job(worker, progress):
		job_started.set()
		release_job.wait(10)
	def preview(worker, progress):
		preview_started.set()
		release_preview.wait(10)
	window._running_job = True
	window._job_task_id = window._submit("Cleanup job", job)
	window._submit("Preview", preview, pool=window.preview_pool)
	qtbot.waitUntil(lambda: job_started.is_set() and preview_started.is_set(), timeout=5000)
	try:
		release_job.set()
		qtbot.waitUntil(lambda: not window._running_job, timeout=5000)
		assert window._job_task_id is None
		assert window.tasks
		assert not release_preview.is_set()
	finally:
		release_preview.set()
		qtbot.waitUntil(lambda: not window.tasks, timeout=5000)
		window.close()


@pytest.mark.parametrize("scheme", ["javascript:alert(1)", "java\nscript:alert(1)", "data:text/html,bad", "file:///private", "//tracker.invalid/x", "/relative"])
def test_preview_allowlist_rejects_active_or_relative_links_and_all_style_attributes(scheme):
	from bs4 import BeautifulSoup
	clean = sanitize_preview_html('<head><script>bad()</script><meta charset="utf-8"></head><!--private-->'
		f'<div><p style="background:url(https://tracker.invalid)" class="x"><a href="{scheme}" onclick="bad()" title="Label">Text</a></p></div>')
	soup = BeautifulSoup(clean, "html.parser")
	assert not soup.find(["script", "head", "meta", "div"])
	assert "private" not in clean and "bad()" not in clean
	assert soup.p.attrs == {}
	assert soup.a.attrs == {"title": "Label"}


def test_preview_preserves_only_explicit_safe_link_attributes():
	from bs4 import BeautifulSoup
	clean = sanitize_preview_html('<a href="https://example.invalid/path" title="Open" target="_blank" style="color:red">Link</a>')
	assert BeautifulSoup(clean, "html.parser").a.attrs == {"href": "https://example.invalid/path", "title": "Open"}


def test_switching_simulation_off_in_settings_keeps_reviewed_action_ids(qtbot, service, monkeypatch):
	window = MainWindow(service)
	qtbot.addWidget(window)
	replanned = []
	def update_config(config):
		service.config = config
	monkeypatch.setattr(service, "update_config", update_config, raising=False)
	monkeypatch.setattr(service, "replan", lambda **kwargs: replanned.append(True), raising=False)
	monkeypatch.setattr(window, "_submit", lambda title, operation, done=None, **kwargs: operation(service, lambda value: None))
	config = Config.from_dict({**service.config.to_dict(), "dry_run": False})
	window._save_settings(config)
	assert replanned == []


def test_retry_refresh_resumes_due_system_pause_and_surfaces_block_reason(qapp, qtbot, service, tmp_path, monkeypatch):
	import json
	window = MainWindow(service, QSettings(str(tmp_path / "system-retry.ini"), QSettings.Format.IniFormat))
	qtbot.addWidget(window)
	payload = {"pause": {"origin": "system", "kind": "network", "reason": "Network recovery pending", "due": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}}
	service.store.execute("INSERT INTO jobs(id,state,payload_json) VALUES('system','paused',?)", (json.dumps(payload),))
	window.current_job_id = "system"
	resumed = []
	monkeypatch.setattr(window, "resume_job", lambda job_id=None: resumed.append(job_id))
	window.refresh(force=True)
	assert resumed == ["system"]
	assert "Network recovery pending" in window.job_status.text()
	window.close()


def test_gui_undo_above_cap_requires_and_passes_explicit_confirmation(qtbot, service, monkeypatch):
	from PyQt6.QtWidgets import QMessageBox
	service.config.max_actions_per_run = 1
	window = MainWindow(service)
	qtbot.addWidget(window)
	monkeypatch.setattr(window, "_selected", lambda table: [{"id": "a"}, {"id": "b"}])
	prompts = []
	def confirm(parent, title, text, *args):
		prompts.append(text)
		return QMessageBox.StandardButton.Yes
	monkeypatch.setattr(QMessageBox, "question", confirm)
	monkeypatch.setattr(QMessageBox, "warning", confirm)
	calls = []
	monkeypatch.setattr(service, "undo", lambda ids, **kwargs: calls.append((ids, kwargs)), raising=False)
	monkeypatch.setattr(window, "_submit", lambda title, operation, done=None, **kwargs: operation(service, lambda value: None))
	window.undo_selected("history")
	assert len(prompts) == 1 and "2" in prompts[0] and "1" in prompts[0]
	assert calls[0][1]["allow_over_limit"] is True


def test_gui_undo_above_cap_decline_makes_no_worker_request(qtbot, service, monkeypatch):
	from PyQt6.QtWidgets import QMessageBox
	service.config.max_actions_per_run = 1
	window = MainWindow(service)
	qtbot.addWidget(window)
	monkeypatch.setattr(QMessageBox, "warning", lambda *args: QMessageBox.StandardButton.No)
	monkeypatch.setattr(window, "_submit", lambda *args, **kwargs: pytest.fail("Declined undo must not start a worker"))
	window._submit_undo(["a", "b"], "Synthetic undo")


def test_gui_simulation_mode_is_bound_to_the_submitted_operation(qtbot, service, monkeypatch):
	window = MainWindow(service)
	qtbot.addWidget(window)
	monkeypatch.setattr(service, "plan_snapshot", lambda **kwargs: {"count": 1, "hash": "a" * 64}, raising=False)
	operations = []
	monkeypatch.setattr(window, "_submit", lambda title, operation, **kwargs: operations.append(operation))
	window.apply_plan(True)
	assert len(operations) == 1
	service.config.dry_run = False
	calls = []
	monkeypatch.setattr(service, "apply_actions", lambda **kwargs: calls.append(kwargs), raising=False)
	operations[0](service, lambda value: None)
	assert calls[0]["expected_dry_run"] is True


def test_every_gui_simulation_task_stays_readonly_with_newer_live_preferences(qapp, tmp_path):
	from gmail_clean.gui import ServiceTask
	from gmail_clean.service import AppService
	service = AppService(data_dir=tmp_path, demo=True, count=2)
	observed = []
	def operation(worker, progress):
		worker.reload_config()
		observed.append((worker.config.dry_run, worker.store.checkpoint("effective_preferences")["dry_run"]))
	try:
		task = ServiceTask("mode-regression", service, operation)
		preferences = service.store.checkpoint("effective_preferences")
		preferences["dry_run"] = False
		service.store.set_checkpoint("effective_preferences", preferences)
		task.run()
		assert observed == [(True, False)]
	finally:
		service.close()


def test_settings_task_can_save_live_preference_while_current_task_stays_readonly(qapp, tmp_path):
	from gmail_clean.gui import ServiceTask
	from gmail_clean.service import AppService
	service = AppService(data_dir=tmp_path, demo=True, count=2)
	observed = []
	def operation(worker, progress):
		config = Config.from_dict({**worker.config.to_dict(), "dry_run": False})
		worker.update_config(config)
		worker.reload_config()
		observed.append((worker.config.dry_run, worker.store.checkpoint("effective_preferences")["dry_run"]))
	try:
		ServiceTask("settings-mode-regression", service, operation).run()
		assert observed == [(True, False)]
		assert service.config.dry_run is True  # The UI changes only after its result callback.
	finally:
		service.close()


@pytest.mark.parametrize("provider_problem", ["changed", "missing"])
def test_readonly_gui_tasks_do_not_validate_or_load_the_external_provider(qapp, tmp_path, monkeypatch, provider_problem):
	from gmail_clean.ai import AIError
	from gmail_clean.gui import ServiceTask
	from gmail_clean.service import AppService
	service = AppService(data_dir=tmp_path / "data", demo=True, count=2)
	provider = tmp_path / "synthetic_provider.py"
	provider.write_text("# approved synthetic source\n", encoding="utf-8")
	if provider_problem == "changed":
		provider.write_text("# synthetic source changed after approval\n", encoding="utf-8")
	else:
		provider.unlink()
	def trust_blocked(*args, **kwargs):
		raise AIError("ModuleTrustError", f"Synthetic provider is {provider_problem}; reapproval is required")
	# Integration injects the same trust failure that the configuration and AI
	# boundaries emit. Read-only operations must not call either boundary.
	monkeypatch.setattr(AppService, "set_session_config", trust_blocked)
	monkeypatch.setattr(AppService, "ai_for", trust_blocked)
	observed = []
	def operation(worker, progress):
		worker.reload_config()
		observed.append((worker.config.dry_run, worker.store.one("SELECT count(*) n FROM messages")["n"]))
		with pytest.raises(AIError, match="reapproval"):
			worker.ai_for()
	try:
		ServiceTask("provider-independent-readonly", service, operation).run()
		assert observed == [(True, 0)]
	finally:
		service.close()


def test_disconnect_and_revoke_is_an_explicit_confirmed_action(qtbot, service, monkeypatch):
	from PyQt6.QtWidgets import QMessageBox, QPushButton
	window = MainWindow(service)
	qtbot.addWidget(window)
	service.demo = False
	buttons = [button for button in window.findChildren(QPushButton) if button.text() == "Disconnect and revoke"]
	assert len(buttons) == 1
	calls = []
	monkeypatch.setattr(window, "_submit", lambda title, operation: calls.append((title, operation)))
	monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
	buttons[0].click()
	assert not calls
	monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
	buttons[0].click()
	assert len(calls) == 1
	fake = type("Connection", (), {"disconnect": lambda self, revoke=False: calls.append(revoke)})()
	calls[0][1](fake, None)
	assert calls[-1] is True


def test_saving_external_ai_code_requires_hash_confirmation(qtbot, tmp_path, monkeypatch):
	from PyQt6.QtWidgets import QMessageBox
	module = tmp_path / "provider.py"
	module.write_text("def call_api(prompt):\n\treturn 'offline'\n")
	editor = SettingsEditor(Config(ai_module=str(module)))
	qtbot.addWidget(editor)
	saved = []
	editor.saved.connect(saved.append)
	questions = []
	def answer_no(*args):
		questions.append(args)
		return QMessageBox.StandardButton.No
	monkeypatch.setattr(QMessageBox, "question", answer_no)
	editor._save()
	assert questions
	assert not saved
	assert "SHA-256" in questions[-1][2]
	monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
	editor._save()
	assert len(saved) == 1
	assert len(saved[0].ai_module_sha256) == 64
	assert "ai_module_sha256" not in editor.controls
