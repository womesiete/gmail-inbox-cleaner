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
from gmail_clean.gui import (LabelPicker, MainWindow, SafePreview, SettingsEditor,
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
