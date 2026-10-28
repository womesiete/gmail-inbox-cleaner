"""PyQt6 presentation: explicit message targets, asynchronous work, and SQL views."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Comment
from PyQt6.QtCore import (QByteArray, QLockFile, QObject, QRunnable, QSettings,
	Qt, QThreadPool, QTimer, QUrl, QItemSelectionModel, pyqtSignal, pyqtSlot)
from PyQt6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
	QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QDoubleSpinBox,
	QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
	QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
	QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
	QScrollArea, QSpinBox, QSplitter, QStatusBar, QTabWidget,
	QTableView, QTextBrowser, QVBoxLayout, QWidget)

from .domain import Config, resolve_age_cutoff, resolve_window
from .table_model import Column, SqlTableModel


UTC = timezone.utc
_DRAINING_WORK = []


class _DrainingWork(QObject):
	"""Keep detached pools alive until their own workers release resources.

	A pool's destructor waits for running QRunnables. Closing a window must not
	delete that pool or a worker-owned SQLite connection from the GUI thread.
	This owner drains independently after the UI has stopped accepting results.
	"""
	def __init__(self, pools, tasks):
		super().__init__()
		self.pools, self.tasks = list(pools), dict(tasks)
		self._drained = False
		for pool in self.pools:
			pool.setParent(None)
		self.timer = QTimer(self)
		self.timer.setInterval(100)
		self.timer.timeout.connect(self._check)
		for task in self.tasks.values():
			task.signals.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
		_DRAINING_WORK.append(self)
		self.timer.start()

	@pyqtSlot(str)
	def _finished(self, task_id):
		self.tasks.pop(task_id, None)
		self._check()

	def _check(self):
		if self._drained:
			return
		if any(pool.activeThreadCount() for pool in self.pools):
			return
		self._drained = True
		self.timer.stop()
		self.tasks.clear()
		for pool in self.pools:
			pool.deleteLater()
		self.pools.clear()
		_DRAINING_WORK.remove(self)
		self.deleteLater()


def preferences(demo: bool = False) -> QSettings:
	return QSettings("GmailInboxCleaner", "Demo" if demo else "Personal")


def load_preferences(settings: QSettings | None = None) -> Config:
	settings = settings or preferences()
	payload = settings.value("preferences/config", "{}")
	try:
		config = Config.from_dict(json.loads(str(payload)))
		config.validate()
		return config
	except (ValueError, TypeError, KeyError):
		return Config()


def save_preferences(config: Config, settings: QSettings | None = None):
	settings = settings or preferences()
	settings.setValue("preferences/config", json.dumps(config.to_dict(), ensure_ascii=False))
	settings.sync()


class InstanceLockError(RuntimeError):
	"""A safe startup diagnostic for an already-owned application directory."""
	def __init__(self):
		self.safe_message = "Gmail Inbox Cleaner is already using this data directory. Close the other instance first."
		super().__init__(self.safe_message)


def acquire_instance_lock(data_dir: str | Path) -> QLockFile:
	"""QLockFile validates the PID/host and recovers abandoned local locks."""
	data_dir = Path(data_dir)
	data_dir.mkdir(parents=True, exist_ok=True)
	lock = QLockFile(str(data_dir / "instance.lock"))
	lock.setStaleLockTime(0)
	if not lock.tryLock(0):
		raise InstanceLockError()
	return lock


def sanitize_preview_html(value: str) -> str:
	"""Strip active content before Qt sees it; resources are independently blocked."""
	soup = BeautifulSoup(value, "html.parser")
	for tag in soup(["script", "style", "iframe", "object", "embed", "form", "input", "img", "svg", "math", "link", "meta", "head", "noscript"]):
		tag.decompose()
	for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
		comment.extract()
	allowed_tags = {"p", "br", "b", "i", "strong", "em",
		"ul", "ol", "li", "blockquote", "pre", "code", "table", "thead", "tbody",
		"tr", "td", "th", "a", "hr", "h1", "h2", "h3"}
	for tag in list(soup.find_all(True)):
		if tag.name not in allowed_tags:
			tag.unwrap()
		elif tag.name == "a":
			tag.attrs = {key: value for key, value in tag.attrs.items() if key in {"href", "title"}}
			if "href" in tag.attrs:
				try:
					allowed = urlsplit(str(tag["href"]).strip()).scheme.casefold() in {"http", "https", "mailto"}
				except ValueError:
					allowed = False
				if not allowed:
					del tag["href"]
		else:
			tag.attrs = {}
	return str(soup)


class SafePreview(QTextBrowser):
	def __init__(self, parent=None):
		super().__init__(parent)
		self.setOpenLinks(False)
		self.setOpenExternalLinks(False)
		self.anchorClicked.connect(self._open_link)

	def loadResource(self, resource_type, name):
		# This includes file:, data:, and remote schemes. Email never gets a loader.
		return QByteArray()

	def set_message(self, text: str, *, html: bool = False):
		if html:
			self.setHtml(sanitize_preview_html(text))
		else:
			self.setPlainText(text)

	@pyqtSlot(QUrl)
	def _open_link(self, url):
		if url.scheme().lower() in {"https", "http", "mailto"}:
			QDesktopServices.openUrl(url)


class TaskSignals(QObject):
	progress = pyqtSignal(str, str)
	result = pyqtSignal(str, object)
	error = pyqtSignal(str, str)
	finished = pyqtSignal(str)


class ServiceTask(QRunnable):
	"""The worker owns its service, transport, and SQLite connection."""
	def __init__(self, task_id, service, operation):
		super().__init__()
		self.task_id, self.service, self.operation = task_id, service, operation
		self.signals = TaskSignals()
		self.started = threading.Event()
		self.cancelled = threading.Event()

	def run(self):
		self.started.set()
		try:
			if self.cancelled.is_set():
				return
			with self.service.worker() as worker:
				try:
					result = self.operation(worker, lambda text: self.signals.progress.emit(self.task_id, str(text)))
				except Exception as exc:
					if hasattr(worker, "error"):
						try:
							worker.error("interface", exc)
						except Exception:
							pass  # Failure reporting must preserve the original exception.
					raise
			self.signals.result.emit(self.task_id, result)
		except Exception as exc:
			# Raw exception payloads may contain provider request text or credentials.
			from .runtime import safe_error
			self.signals.error.emit(self.task_id, safe_error(exc))
		finally:
			self.signals.finished.emit(self.task_id)


class LabelPicker(QDialog):
	"""Choose additions or explicit suggestion corrections by existing user label ID."""
	def __init__(self, labels, *, selected=(), current=(), corrections=False, parent=None):
		super().__init__(parent)
		self.setWindowTitle("Correct suggested labels" if corrections else "Add existing labels")
		self.resize(440, 480)
		layout = QVBoxLayout(self)
		text = ("Choose unapplied suggestions to reject. Existing Gmail labels are preserved."
			if corrections else "Checked labels will be added. Existing labels remain attached.")
		layout.addWidget(QLabel(text, wordWrap=True))
		self.search = QLineEdit(placeholderText="Search user labels…")
		layout.addWidget(self.search)
		self.list = QListWidget()
		selected, current = set(selected), set(current)
		for label in sorted(labels, key=lambda item: item.get("name", "").casefold()):
			if str(label.get("type", "user")).lower() != "user":
				continue
			label_id = str(label["id"])
			caption = f"{label['name']}" + (" · already attached" if label_id in current else "")
			item = QListWidgetItem(caption)
			item.setData(Qt.ItemDataRole.UserRole, label_id)
			item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
			item.setCheckState(Qt.CheckState.Checked if label_id in selected else Qt.CheckState.Unchecked)
			self.list.addItem(item)
		layout.addWidget(self.list)
		self.search.textChanged.connect(self._search)
		buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
		buttons.accepted.connect(self.accept)
		buttons.rejected.connect(self.reject)
		layout.addWidget(buttons)

	def _search(self, text):
		for row in range(self.list.count()):
			item = self.list.item(row)
			item.setHidden(text.casefold() not in item.text().casefold())

	def chosen_ids(self):
		return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
			if self.list.item(i).checkState() == Qt.CheckState.Checked]


class RuleDialog(QDialog):
	previewRequested = pyqtSignal(object)

	def __init__(self, rule=None, parent=None):
		super().__init__(parent)
		self.rule = dict(rule or {})
		self.setWindowTitle("Edit explicit rule" if rule else "Create explicit rule")
		self.resize(560, 430)
		layout = QVBoxLayout(self)
		layout.addWidget(QLabel("Use a recurring list/type/template or sender plus a condition. Review sample matches before enabling an archive rule.", wordWrap=True))
		form = QFormLayout()
		self.name = QLineEdit(self.rule.get("name", ""))
		self.action = QComboBox()
		self.action.addItems(["keep", "archive", "review"])
		self.action.setCurrentText(self.rule.get("action", "keep"))
		self.enabled = QCheckBox("Enabled")
		self.enabled.setChecked(bool(self.rule.get("enabled", False)))
		form.addRow("Name", self.name)
		form.addRow("Recommendation", self.action)
		form.addRow("Activation", self.enabled)
		layout.addLayout(form)
		layout.addWidget(QLabel('Conditions as JSON: sender, domain, list_id, subject_contains, category, template. All specified conditions must match.'))
		self.conditions = QPlainTextEdit(json.dumps(self.rule.get("conditions", {}), indent=2))
		layout.addWidget(self.conditions)
		preview = QPushButton("Preview eligible matches and exceptions")
		preview.clicked.connect(self._preview_matches)
		layout.addWidget(preview)
		self.summary = QLabel("", wordWrap=True)
		layout.addWidget(self.summary)
		buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
		buttons.accepted.connect(self._save)
		buttons.rejected.connect(self.reject)
		layout.addWidget(buttons)

	def _preview_matches(self):
		try:
			conditions = json.loads(self.conditions.toPlainText())
			if not isinstance(conditions, dict) or not conditions:
				raise ValueError("Provide nonempty JSON conditions first.")
			self.previewRequested.emit(conditions)
		except (ValueError, TypeError) as exc:
			self.summary.setText(str(exc))

	def _save(self):
		try:
			conditions = json.loads(self.conditions.toPlainText())
			if not isinstance(conditions, dict) or not conditions or not self.name.text().strip():
				raise ValueError("A name and at least one condition are required.")
			if self.action.currentText() == "archive" and not ("template" in conditions or "list_id" in conditions or ("sender" in conditions and len(conditions) >= 2)):
				raise ValueError("Add a list, category, subject, or template condition to narrow archival.")
			self.rule.update(name=self.name.text().strip(), action=self.action.currentText(),
				conditions=conditions, enabled=self.enabled.isChecked(), source="explicit")
			self.accept()
		except (ValueError, TypeError) as exc:
			self.summary.setText(str(exc))


class SettingsEditor(QWidget):
	"""Typed controls for the complete Config schema, persisted using QSettings."""
	saved = pyqtSignal(object)

	GROUPS = [
		("Examination window", ["timezone", "earliest_date", "latest_mode", "latest_days", "latest_date"]),
		("Archival policy", ["age_enabled", "age_months", "age_days", "category_age_overrides", "archive_threshold", "keep_threshold", "age_archive_max_score"]),
		("Protection and labels", ["protect_starred", "protected_senders", "protected_domains", "protected_label_ids", "label_meanings"]),
		("Execution and learning", ["dry_run", "auto_execute", "auto_label", "learning_enabled"]),
		("External AI", ["ai_module", "ai_model", "ai_use_call_api_result", "ai_batch_size", "ai_timeout_seconds", "optional_model_routes", "estimated_cost_per_call"]),
		("Content sharing and context", ["excluded_senders", "excluded_domains", "excluded_label_ids", "sent_context_enabled", "archived_context_enabled", "download_all_bodies", "body_limit"]),
		("Jobs and local data", ["retrieval_batch_size", "execution_batch_size", "max_retries", "retry_base_seconds", "maintenance_interval_minutes", "reconciliation_interval_hours", "retention_days", "log_level", "shortcuts"]),
	]

	LABELS = {
		"timezone": "Calendar/display timezone", "earliest_date": "Inclusive first date · blank = unlimited",
		"latest_mode": "Latest examination bound", "latest_days": "Rolling exclusion · 24-hour days",
		"latest_date": "Fixed inclusive last date · YYYY-MM-DD", "age_enabled": "Enable age-based archival",
		"age_months": "Global age · calendar months", "age_days": "Additional age · days",
		"category_age_overrides": "Category overrides · JSON", "archive_threshold": "Archive score below",
		"keep_threshold": "Keep score at/above", "age_archive_max_score": "Old-mail archive score below",
		"dry_run": "Simulation · no Gmail mutations", "auto_execute": "Execute approved automatic plans during a run",
		"auto_label": "Allow automatic existing-label additions", "learning_enabled": "Use and collect reviewed learning examples",
		"ai_module": "Module package or standalone .py path", "ai_model": "Exact model identifier · blank = module default",
		"ai_use_call_api_result": "Use optional call_api_result metadata", "ai_batch_size": "Messages per prompt · 1–10",
		"ai_timeout_seconds": "Local worker watchdog · seconds", "protect_starred": "Protect starred messages",
		"excluded_senders": "Do not share content · sender addresses", "excluded_domains": "Do not share content · domains",
		"excluded_label_ids": "Do not share content · label IDs", "label_meanings": "Label ID → protected/reference/neutral · JSON",
		"protected_label_ids": "Protect existing label IDs", "protected_senders": "Protect sender addresses", "protected_domains": "Protect sender domains",
		"shortcuts": "Review shortcuts · action → key sequence JSON", "maintenance_interval_minutes": "Optional in-app maintenance interval · minutes",
		"body_limit": "Maximum normalized body characters", "retention_days": "Body-cache retention · days",
	}

	def __init__(self, config, parent=None, *, demo=False):
		super().__init__(parent)
		self.config = config
		self.demo = demo
		self.controls = {}
		outer = QVBoxLayout(self)
		help_text = QLabel("The examination window selects targets; the age policy decides what to recommend. Default: unlimited earliest date, exclude the latest 14 × 24 hours, then apply a separate two-calendar-month archival policy. Scope/protection changes invalidate pending actions. Secrets belong to your external AI module and protected OAuth storage.", wordWrap=True)
		outer.addWidget(help_text)
		if demo:
			outer.addWidget(QLabel("Offline demo uses synthetic AI only. Personal mode supports your external module; demo data never enters it.", wordWrap=True))
		self.resolved = QLabel("", wordWrap=True)
		outer.addWidget(self.resolved)
		scroll = QScrollArea()
		scroll.setWidgetResizable(True)
		container = QWidget()
		groups_layout = QVBoxLayout(container)
		values = config.to_dict()
		included = set()
		groups = list(self.GROUPS)
		remaining = [key for key in values if key not in {k for _, keys in groups for k in keys} and key != "extras"]
		if remaining:
			groups.append(("Additional preferences", remaining))
		for title, keys in groups:
			box = QGroupBox(title)
			form = QFormLayout(box)
			form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
			for key in keys:
				if key not in values:
					continue
				included.add(key)
				widget = self._control(key, values[key])
				self.controls[key] = (widget, type(values[key]))
				if demo and key == "ai_module":
					widget.setEnabled(False)
				form.addRow(self.LABELS.get(key, key.replace("_", " ").capitalize()), widget)
			groups_layout.addWidget(box)
		groups_layout.addStretch()
		scroll.setWidget(container)
		outer.addWidget(scroll)
		buttons = QHBoxLayout()
		self.error = QLabel("", wordWrap=True)
		buttons.addWidget(self.error, 1)
		preview = QPushButton("Preview resolved dates")
		preview.clicked.connect(self._preview)
		buttons.addWidget(preview)
		save = QPushButton("Save settings and replan")
		save.clicked.connect(self._save)
		buttons.addWidget(save)
		outer.addLayout(buttons)
		self._preview()

	def _control(self, key, value):
		if isinstance(value, bool):
			widget = QCheckBox()
			widget.setChecked(value)
		elif isinstance(value, int):
			widget = QSpinBox()
			limits = {"ai_batch_size": (1, 10), "retrieval_batch_size": (1, 50),
				"execution_batch_size": (1, 500), "archive_threshold": (0, 100),
				"keep_threshold": (0, 100), "age_archive_max_score": (0, 100)}
			widget.setRange(*limits.get(key, (0, 10_000_000)))
			widget.setValue(value)
		elif isinstance(value, float):
			widget = QDoubleSpinBox()
			widget.setRange(0, 1_000_000)
			widget.setDecimals(5)
			widget.setValue(value)
		elif isinstance(value, (dict, list)):
			widget = QPlainTextEdit(json.dumps(value, indent=2, ensure_ascii=False))
			widget.setMaximumHeight(95 if isinstance(value, dict) else 65)
		elif key in {"latest_mode", "log_level"}:
			widget = QComboBox()
			widget.addItems(["rolling", "fixed"] if key == "latest_mode" else ["DEBUG", "INFO", "WARNING", "ERROR"])
			widget.setCurrentText(str(value))
		else:
			widget = QLineEdit("" if value is None else str(value))
			if key.endswith("date"):
				widget.setPlaceholderText("YYYY-MM-DD · blank when unused")
		widget.setObjectName(f"setting_{key}")
		return widget

	def read_config(self):
		values = self.config.to_dict()
		for key, (widget, value_type) in self.controls.items():
			if isinstance(widget, QCheckBox):
				value = widget.isChecked()
			elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
				value = widget.value()
			elif isinstance(widget, QPlainTextEdit):
				value = json.loads(widget.toPlainText())
				if not isinstance(value, value_type):
					raise ValueError(f"{key} requires a JSON {value_type.__name__}.")
			elif isinstance(widget, QComboBox):
				value = widget.currentText()
			else:
				value = widget.text().strip()
				if key in {"earliest_date", "latest_date"} and not value:
					value = None
			values[key] = value
		config = Config.from_dict(values)
		config.validate()
		return config

	def _preview(self):
		try:
			config = self.read_config()
			window = resolve_window(config, datetime.now(UTC))
			cutoff = resolve_age_cutoff(config, window.anchor_utc)
			self.resolved.setText(format_window(window, cutoff))
			self.error.clear()
		except (ValueError, TypeError, KeyError) as exc:
			self.error.setText(str(exc))

	def _save(self):
		try:
			config = self.read_config()
			self._preview()
			self.saved.emit(config)
		except (ValueError, TypeError, KeyError) as exc:
			self.error.setText(str(exc))


def format_window(window, cutoff=None):
	zone = ZoneInfo(window.timezone)
	def stamp(value):
		if value is None:
			return "unlimited"
		return value.astimezone(zone).isoformat(timespec="seconds")
	text = (f"Examine [{stamp(window.earliest_inclusive)}, {stamp(window.latest_exclusive)}) · {window.timezone}\n"
		f"UTC: [{window.earliest_inclusive.isoformat() if window.earliest_inclusive else 'unlimited'}, {window.latest_exclusive.isoformat()})")
	if cutoff is not None:
		text += f"\nAge policy: old means internalDate strictly before {stamp(cutoff)}"
	else:
		text += "\nGlobal age-based archival is disabled; category overrides may apply."
	return text


class MainWindow(QMainWindow):
	"""Desktop workflow; network/AI/action execution always uses a worker service."""
	DEFAULT_SHORTCUTS = {"archive": "A", "keep": "K", "defer": "D", "label": "L", "undo": "Ctrl+Z", "open": "O"}

	def __init__(self, service, settings=None):
		super().__init__()
		self.service = service
		self.settings = settings or preferences(bool(service.demo))
		self.pool = QThreadPool(self)
		self.pool.setMaxThreadCount(1)
		self.preview_pool = QThreadPool(self)
		self.preview_pool.setMaxThreadCount(1)
		self.tasks, self.callbacks = {}, {}
		self.models, self.views, self.model_specs = {}, {}, {}
		self.stop_event = threading.Event()
		self._requested_control_state = None
		self._job_task_id = None
		self.current_job_id = None
		self._running_job = False
		self._closing = False
		self._resources_closed = False
		self.shutdown_incomplete = False
		self._close_deadline = QTimer(self)
		self._close_deadline.setSingleShot(True)
		self._close_deadline.timeout.connect(self._force_close)
		self._last_action_ids = []
		self._reader = None
		self.setWindowTitle("Gmail Inbox Cleaner" + (" · Offline demo" if service.demo else ""))
		self.resize(1440, 930)
		root = QWidget()
		self.setCentralWidget(root)
		layout = QVBoxLayout(root)
		self.banner = QLabel()
		self.banner.setWordWrap(True)
		self.banner.setStyleSheet("padding: 10px; background: #e9f2f7; color: #183c50; font-weight: 600;")
		layout.addWidget(self.banner)
		self.tabs = QTabWidget()
		layout.addWidget(self.tabs)
		self._build_overview()
		self._build_review()
		self._build_plan()
		self._build_learning()
		self._build_history()
		self.settings_editor = SettingsEditor(service.config, demo=bool(service.demo))
		self.settings_editor.saved.connect(self._save_settings)
		self.tabs.addTab(self.settings_editor, "Settings")
		self._build_errors()
		self.setStatusBar(QStatusBar())
		self._connect_reader()
		latest = self._query("SELECT id FROM jobs ORDER BY created_at DESC LIMIT 1")
		if latest:
			self.current_job_id = latest[0]["id"]
			self._filter_review()
			self._refresh_plan_groups()
			for row_number in self.models["jobs"].rows_for_ids([self.current_job_id]):
				self.views["jobs"].selectRow(row_number)
		self._setup_shortcuts()
		QApplication.instance().focusChanged.connect(self._focus_changed)
		geometry = self.settings.value("window/geometry")
		if geometry:
			self.restoreGeometry(geometry)
		for key, view in self.views.items():
			state = self.settings.value(f"tables/{key}/header")
			if state:
				view.horizontalHeader().restoreState(state)
		self.refresh_timer = QTimer(self)
		self.refresh_timer.setInterval(1500)
		self.refresh_timer.timeout.connect(self.refresh)
		self.refresh_timer.start()
		self.maintenance_timer = QTimer(self)
		self.maintenance_timer.timeout.connect(self._maintenance_tick)
		self._configure_maintenance()
		self.refresh()

	def _tab(self, title):
		widget = QWidget()
		layout = QVBoxLayout(widget)
		self.tabs.addTab(widget, title)
		return layout

	def _button(self, text, slot, *, tooltip=""):
		button = QPushButton(text)
		button.clicked.connect(slot)
		if tooltip:
			button.setToolTip(tooltip)
		return button

	def _table(self, key, query, columns, *, id_column="id"):
		view = QTableView()
		view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
		view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
		view.setAlternatingRowColors(True)
		view.setSortingEnabled(True)
		view.setWordWrap(False)
		view.setShowGrid(False)
		view.verticalHeader().setDefaultSectionSize(27)
		view.verticalHeader().setVisible(False)
		self.views[key] = view
		self.model_specs[key] = (query, columns, id_column)
		return view

	def _date(self, value):
		try:
			return datetime.fromtimestamp(int(value) / 1000, UTC).astimezone(ZoneInfo(self.service.config.timezone)).strftime("%Y-%m-%d %H:%M")
		except (ValueError, TypeError, OverflowError):
			return str(value)

	def _build_overview(self):
		layout = self._tab("Overview / Jobs")
		self.account_label = QLabel("", wordWrap=True)
		layout.addWidget(self.account_label)
		self.window_label = QLabel("", wordWrap=True)
		layout.addWidget(self.window_label)
		bar = QHBoxLayout()
		bar.addWidget(self._button("Connect Gmail…", self._connect_account))
		bar.addWidget(self._button("Disconnect", self._disconnect_account))
		bar.addWidget(self._button("Refresh labels", lambda: self._submit("Refresh labels", lambda svc, p: svc.refresh_labels())))
		bar.addStretch()
		layout.addLayout(bar)
		controls = QHBoxLayout()
		self.start_button = self._button("Start new cleanup", self.start_job)
		controls.addWidget(self.start_button)
		controls.addWidget(self._button("Pause after current work", lambda: self._control("paused")))
		controls.addWidget(self._button("Resume selected job", self.resume_job))
		controls.addWidget(self._button("Stop and preserve progress", lambda: self._control("stopped")))
		layout.addLayout(controls)
		self.progress = QProgressBar()
		self.progress.setRange(0, 1)
		self.progress.setValue(0)
		layout.addWidget(self.progress)
		self.job_status = QLabel("Ready. Start a cleanup to discover and assess eligible inbox messages.", wordWrap=True)
		layout.addWidget(self.job_status)
		jobs = self._table("jobs", "SELECT id, state, stage, anchor_utc, created_at, updated_at FROM jobs",
			[Column("id", "Job", 260), Column("state", "State", 110), Column("stage", "Stage", 130),
			Column("anchor_utc", "Frozen anchor (UTC)", 240), Column("updated_at", "Last update (UTC)", 240)])
		jobs.clicked.connect(self._select_job)
		layout.addWidget(jobs)
		self.stage_counts = QLabel("", wordWrap=True)
		layout.addWidget(self.stage_counts)
		self.maintenance_label = QLabel("", wordWrap=True)
		layout.addWidget(self.maintenance_label)
		data_bar = QHBoxLayout()
		data_bar.addWidget(self._button("Backup database…", self._backup))
		data_bar.addWidget(self._button("Apply cache retention", lambda: self._submit("Apply retention", lambda svc, p: svc.retention())))
		data_bar.addStretch()
		self.in_app_maintenance = QCheckBox("Run maintenance while this window is open")
		self.in_app_maintenance.setChecked(self.settings.value("maintenance/enabled", False, type=bool))
		self.in_app_maintenance.toggled.connect(self._configure_maintenance)
		data_bar.addWidget(self.in_app_maintenance)
		layout.addLayout(data_bar)

	def _build_review(self):
		layout = self._tab("Review")
		filters = QHBoxLayout()
		self.review_search = QLineEdit(placeholderText="Search sender, subject, or message ID…")
		self.review_search.setText(self.settings.value("review/search", ""))
		self.review_decision = QComboBox()
		self.review_decision.addItems(["All inbox decisions", "Needs review", "Archive", "Keep", "Deferred"])
		filters.addWidget(self.review_search, 2)
		filters.addWidget(self.review_decision)
		self.group_by = QComboBox()
		self.group_by.addItems(["Individual messages", "Group by sender", "Group by category", "Group by conversation", "Group by list"])
		filters.addWidget(self.group_by)
		layout.addLayout(filters)
		self.group_table = self._table("groups",
			"SELECT sender AS id, sender AS group_name, COUNT(*) AS count, MIN(internal_ms) AS oldest, MAX(internal_ms) AS newest, COUNT(DISTINCT decision) AS outcomes FROM messages WHERE in_inbox=1 GROUP BY sender",
			[Column("group_name", "Sender", 380), Column("count", "Messages", 90), Column("oldest", "Oldest", 160, self._date), Column("newest", "Newest", 160, self._date), Column("outcomes", "Different outcomes", 140)])
		self.group_table.setMaximumHeight(180)
		self.group_table.hide()
		self.group_table.clicked.connect(self._select_group)
		layout.addWidget(self.group_table)
		self.group_hint = QLabel("Actions target only selected displayed eligible message IDs. Mixed groups retain their exceptions.", wordWrap=True)
		layout.addWidget(self.group_hint)
		bar = QHBoxLayout()
		self.review_buttons = {}
		for text, action in [("Archive [A]", "archive"), ("Keep / needs attention [K]", "keep"), ("Defer [D]…", "defer"), ("Add labels [L]…", "label")]:
			button = self._button(text, lambda checked=False, action=action: self.review_action(action))
			self.review_buttons[action] = button
			bar.addWidget(button)
		bar.addWidget(self._button("Correct suggestions…", lambda: self.review_action("reject_label")))
		open_button = self._button("Open in Gmail [O]", self.open_gmail)
		self.review_buttons["open"] = open_button
		bar.addWidget(open_button)
		layout.addLayout(bar)
		options = QHBoxLayout()
		self.reason = QComboBox()
		self.reason.addItems(["Optional reason", "Completed", "Expired", "Not relevant", "Mistaken automatic archive"])
		options.addWidget(self.reason)
		self.review_scope = QComboBox()
		self.review_scope.addItem("Selected messages · default", "message")
		self.review_scope.addItem("Selected eligible conversation items", "conversation")
		options.addWidget(self.review_scope)
		options.addWidget(self._button("Create an explicit future rule…", self._new_rule))
		options.addWidget(self._button("Fetch selected text", self._fetch_selected_text))
		options.addStretch()
		layout.addLayout(options)
		self.review_splitter = QSplitter(Qt.Orientation.Vertical)
		query = """SELECT m.id, m.thread_id, m.internal_ms, m.sender, m.subject, m.category,
			m.decision, m.labels_json, m.headers_json, m.context_version, m.defer_until_utc,
			COALESCE(json_extract(m.headers_json,'$.List-ID'),json_extract(m.headers_json,'$.list-id'),'(no list)') AS list_id,
			json_extract(p.assessment_json,'$.importance') AS importance,
			json_extract(p.assessment_json,'$.outstanding_action') AS attention,
			json_extract(p.assessment_json,'$.keep_in_inbox_score') AS score,
			json_extract(p.assessment_json,'$.uncertainty') AS uncertainty,
			p.reason, p.assessment_json, json_extract(p.assessment_json,'$.suggested_label_ids') AS suggested_labels, m.in_inbox
			FROM messages m LEFT JOIN predictions p ON p.id=(SELECT pp.id FROM predictions pp
			WHERE pp.message_id=m.id AND pp.valid=1 ORDER BY pp.created_at DESC LIMIT 1)"""
		review = self._table("review", query,
			[Column("sender", "Sender", 200), Column("subject", "Subject", 330), Column("internal_ms", "Received", 155, self._date),
			Column("category", "Category", 110), Column("decision", "Recommendation", 120), Column("labels_json", "Current label IDs / markers", 190), Column("suggested_labels", "Suggested label IDs", 170), Column("importance", "Lasting importance", 115),
			Column("attention", "Outstanding action", 140), Column("score", "Inbox score", 90), Column("uncertainty", "Uncertainty", 95), Column("reason", "Reason", 330)])
		review.clicked.connect(self._show_preview)
		self.review_splitter.addWidget(review)
		preview_widget = QWidget()
		preview_layout = QVBoxLayout(preview_widget)
		self.selection_label = QLabel("Select messages to inspect their explicit scope.", wordWrap=True)
		preview_layout.addWidget(self.selection_label)
		self.preview = SafePreview()
		self.preview.setPlainText("Select a message. Previewing cached text does not mark it read.")
		preview_layout.addWidget(self.preview)
		self.review_splitter.addWidget(preview_widget)
		self.review_splitter.setSizes([440, 220])
		state = self.settings.value("review/splitter")
		if state:
			self.review_splitter.restoreState(state)
		layout.addWidget(self.review_splitter, 1)
		self.review_search.textChanged.connect(self._filter_review)
		self.review_decision.currentIndexChanged.connect(self._filter_review)
		self.group_by.currentIndexChanged.connect(self._group_changed)
		self._group_filter = None

	def _build_plan(self):
		layout = self._tab("Plan / Simulation")
		self.plan_summary = QLabel("", wordWrap=True)
		layout.addWidget(self.plan_summary)
		layout.addWidget(QLabel("Current eligible recommendations by category and age · sender/conversation breakdowns are available in Review."))
		groups = self._table("plan_groups", self._plan_group_query(),
			[Column("category", "Category", 140), Column("decision", "Recommendation", 130), Column("age_bucket", "Resolved age policy", 170),
			Column("count", "Messages", 100), Column("senders", "Senders", 100), Column("oldest", "Oldest", 170, self._date), Column("newest", "Newest", 170, self._date)])
		groups.setMaximumHeight(200)
		layout.addWidget(groups)
		bar = QHBoxLayout()
		bar.addWidget(self._button("Recompute recommendations", lambda: self._submit("Replan", lambda svc, p: svc.replan(should_stop=self.stop_event.is_set))))
		bar.addWidget(self._button("Execute selected plan", lambda: self.apply_plan(False)))
		bar.addWidget(self._button("Execute this plan", lambda: self.apply_plan(True)))
		bar.addWidget(self._button("Cancel selected / undo queued", lambda: self.undo_selected("plan")))
		bar.addStretch()
		self.plan_filter = QComboBox()
		self.plan_filter.addItems(["All proposals", "Queued", "Stale / conflicts", "Archive", "Label"])
		self.plan_filter.currentIndexChanged.connect(self._filter_plan)
		bar.addWidget(self.plan_filter)
		layout.addLayout(bar)
		query = """SELECT a.id,a.message_id,a.job_id,a.state,a.action,a.add_json,a.remove_json,
			a.approval,a.reason,a.created_at,m.sender,m.subject,m.category,m.internal_ms
			FROM actions a LEFT JOIN messages m ON m.id=a.message_id WHERE a.undo_of IS NULL"""
		layout.addWidget(self._table("plan", query,
			[Column("state", "State", 110), Column("action", "Action", 90), Column("sender", "Sender", 200),
			Column("subject", "Subject", 280), Column("internal_ms", "Received", 150, self._date), Column("category", "Category", 110),
			Column("message_id", "Explicit message ID", 190), Column("add_json", "Label additions", 170),
			Column("remove_json", "Label removals", 150), Column("approval", "Approval", 110), Column("reason", "Evidence", 330)]))

	def _plan_group_query(self):
		from .domain import CATEGORIES
		config = self.service.config
		anchor = datetime.now(UTC)
		if self.current_job_id and self._reader:
			jobs = self._query("SELECT anchor_utc FROM jobs WHERE id=?", (self.current_job_id,))
			if jobs:
				anchor = datetime.fromisoformat(jobs[0]["anchor_utc"].replace("Z", "+00:00"))
		def bucket(category):
			try:
				cutoff = resolve_age_cutoff(config, anchor, category)
				return (f"CASE WHEN internal_ms < {int(cutoff.timestamp() * 1000)} THEN 'older than cutoff' ELSE 'at/newer than cutoff' END" if cutoff else "'age policy disabled'")
			except ValueError:
				return "'policy conflict: review'"
		expression = "CASE " + " ".join(f"WHEN category='{category}' THEN {bucket(category)}" for category in sorted(CATEGORIES)) + f" ELSE {bucket(None)} END"
		return (f"SELECT json_array(category,decision,age_bucket) AS id,category,decision,age_bucket,COUNT(*) AS count,COUNT(DISTINCT sender) AS senders,MIN(internal_ms) AS oldest,MAX(internal_ms) AS newest "
			f"FROM (SELECT category,decision,sender,internal_ms,{expression} AS age_bucket FROM messages WHERE {self._group_scope_sql()}) GROUP BY category,decision,age_bucket")

	def _refresh_plan_groups(self):
		if "plan_groups" in self.models:
			query, columns, id_column = self.model_specs["plan_groups"]
			self.model_specs["plan_groups"] = self._plan_group_query(), columns, id_column
			self._replace_model("plan_groups")

	def _build_learning(self):
		layout = self._tab("Learning / Rules")
		self.learning_summary = QLabel("Explicit current-message decisions stay separate from reusable rules and learned examples. Inferred archive rules require enablement.", wordWrap=True)
		layout.addWidget(self.learning_summary)
		bar = QHBoxLayout()
		for text, slot in [("New explicit rule…", self._new_rule), ("Edit selected…", self._edit_rule),
			("Enable selected", lambda: self._toggle_rules(True)), ("Disable selected", lambda: self._toggle_rules(False)),
			("Export rules…", self._export_rules), ("Import rules…", self._import_rules), ("Reset learning…", self._reset_learning)]:
			bar.addWidget(self._button(text, slot))
		layout.addLayout(bar)
		layout.addWidget(self._table("rules", "SELECT id,name,conditions_json,action,enabled,source,support,counterexamples,revision FROM rules",
			[Column("name", "Rule", 220), Column("enabled", "Enabled", 70), Column("source", "Source", 100),
			Column("conditions_json", "All conditions", 360), Column("action", "Action", 90),
			Column("support", "Reviewed support", 110), Column("counterexamples", "Counterexamples", 120), Column("revision", "Revision", 80)]))
		layout.addWidget(QLabel("Reviewed examples · adding a label never creates negatives for other labels."))
		example_bar = QHBoxLayout()
		example_bar.addWidget(self._button("Forget selected examples", self._forget_examples))
		example_bar.addStretch()
		layout.addLayout(example_bar)
		layout.addWidget(self._table("examples", "SELECT id,message_id,sender,category,action,reason,group_id,created_at FROM examples",
			[Column("sender", "Sender", 220), Column("category", "Category", 100), Column("action", "Reviewed action", 130),
			Column("reason", "Reason", 240), Column("group_id", "Review event", 180), Column("message_id", "Message ID", 180)]))

	def _build_history(self):
		layout = self._tab("History / Undo")
		layout.addWidget(QLabel("Undo reverses recorded application-owned deltas on the original message IDs. Later manual changes and deleted labels may require conflict review. Out-of-current-window undo targets remain explicit.", wordWrap=True))
		bar = QHBoxLayout()
		bar.addWidget(self._button("Undo selected actions", lambda: self.undo_selected("history")))
		bar.addWidget(self._button("Undo selected cleanup job", self._undo_job))
		bar.addStretch()
		self.history_search = QLineEdit(placeholderText="Filter history by message ID, job ID, or reason…")
		self.history_search.textChanged.connect(self._filter_history)
		bar.addWidget(self.history_search, 1)
		layout.addLayout(bar)
		self.undo_scope = QLabel("Select original action receipts to preview undo scope.", wordWrap=True)
		layout.addWidget(self.undo_scope)
		layout.addWidget(self._table("history", "SELECT a.id,a.job_id,a.message_id,a.state,a.action,a.add_json,a.remove_json,a.reason,a.approval,a.undo_of,a.created_at,a.updated_at,m.internal_ms FROM actions a LEFT JOIN messages m ON m.id=a.message_id",
			[Column("updated_at", "Updated (UTC)", 210), Column("state", "Outcome", 110), Column("action", "Action", 90),
			Column("message_id", "Message ID", 180), Column("internal_ms", "Received", 150, self._date), Column("job_id", "Job", 200), Column("add_json", "Added", 160),
			Column("remove_json", "Removed", 130), Column("approval", "Approval", 110), Column("reason", "Reason / conflict", 330), Column("undo_of", "Reverses", 190)]))

	def _build_errors(self):
		layout = self._tab("Errors / Log")
		layout.addWidget(QLabel("Errors contain sanitized summaries. Email bodies, provider secrets, OAuth tokens, and full prompts are excluded from routine logging.", wordWrap=True))
		bar = QHBoxLayout()
		self.error_search = QLineEdit(placeholderText="Filter stage, kind, state, or summary…")
		self.error_search.textChanged.connect(self._filter_errors)
		bar.addWidget(self.error_search, 1)
		bar.addWidget(self._button("Retry eligible errors", lambda: self._submit("Retry errors", lambda svc, p: svc.retry_errors())))
		layout.addLayout(bar)
		layout.addWidget(self._table("errors", "SELECT id,job_id,message_id,stage,kind,class_name,summary,attempts,next_attempt_utc,state,created_at FROM errors",
			[Column("created_at", "Observed (UTC)", 205), Column("stage", "Stage", 100), Column("kind", "Kind", 130),
			Column("class_name", "Exception class", 170), Column("summary", "Sanitized summary", 400), Column("state", "State", 100),
			Column("attempts", "Attempts", 75), Column("next_attempt_utc", "Next retry (UTC)", 205), Column("message_id", "Message", 180)]))

	def _connect_reader(self):
		if self._reader:
			self._reader.close()
		self._reader = sqlite3.connect(f"file:{Path(self.service.store.path).as_posix()}?mode=ro", uri=True)
		self._reader.row_factory = sqlite3.Row
		self._reader.execute("PRAGMA busy_timeout=5000")
		for key in self.model_specs:
			self._replace_model(key)
		self._filter_review()
		self._refresh_plan_groups()
		self.views["review"].selectionModel().selectionChanged.connect(self._selection_changed)
		self.views["history"].selectionModel().selectionChanged.connect(self._undo_scope_changed)

	def _replace_model(self, key):
		old = self.models.get(key)
		header_state = self.views[key].horizontalHeader().saveState() if old else self.settings.value(f"tables/{key}/header")
		query, columns, id_column = self.model_specs[key]
		model = SqlTableModel(self.service.store.path, query, columns, id_column=id_column, parent=self)
		self.models[key] = model
		self.views[key].setModel(model)
		for column, spec in enumerate(columns):
			self.views[key].setColumnWidth(column, spec.width)
		if header_state:
			self.views[key].horizontalHeader().restoreState(header_state)
		if old:
			old.close()
			old.deleteLater()

	def _query(self, query, params=()):
		try:
			return [dict(row) for row in self._reader.execute(query, params).fetchall()]
		except sqlite3.Error:
			return []

	def _selected(self, table):
		view = self.views[table]
		selection = view.selectionModel()
		if not selection:
			return []
		return [self.models[table].record(index.row()) for index in selection.selectedRows()]

	def _selected_ids(self, table):
		return [row["id"] for row in self._selected(table) if row.get("id")]

	def _preserve_selection_refresh(self, key):
		# Resetting every timer tick would discard the user's selection. Refresh
		# only when SQLite's data_version reports another connection committed.
		self.models[key].refresh()

	def refresh(self, force=False):
		if self._closing:
			return
		try:
			version = self._reader.execute("PRAGMA data_version").fetchone()[0]
		except sqlite3.Error:
			return
		changed = force or version != getattr(self, "_data_version", None)
		self._data_version = version
		if changed:
			for key, model in self.models.items():
				selected_ids = self._selected_ids(key)
				model.refresh()
				selection = self.views[key].selectionModel()
				for row_number in model.rows_for_ids(selected_ids):
					selection.select(model.index(row_number, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
		self.banner.setText(("OFFLINE DEMO · synthetic mailbox only · " if self.service.demo else "") +
			("SIMULATION · proposals and feedback only" if self.service.config.dry_run else "LIVE · approved explicit message changes may be applied"))
		self.account_label.setText(f"Account: {self.service.account_id or 'Not connected'} · Local data: {self.service.store.path}")
		if self.in_app_maintenance.isChecked() and self.maintenance_timer.isActive():
			remaining = max(0, self.maintenance_timer.remainingTime())
			due = datetime.now(UTC) + timedelta(milliseconds=remaining)
			self.maintenance_label.setText(f"In-app maintenance enabled · next check {due.astimezone(ZoneInfo(self.service.config.timezone)).isoformat(timespec='seconds')} · busy or paused jobs defer the check.")
		else:
			last = self._query("SELECT MAX(created_at) latest FROM jobs")
			if last and last[0].get("latest"):
				try:
					due = datetime.fromisoformat(last[0]["latest"].replace("Z", "+00:00")) + timedelta(minutes=self.service.config.maintenance_interval_minutes)
					self.maintenance_label.setText(f"Manual maintenance · interval reference {due.astimezone(ZoneInfo(self.service.config.timezone)).isoformat(timespec='seconds')} · Start new cleanup when ready.")
				except ValueError:
					self.maintenance_label.setText("Manual maintenance · Start new cleanup when ready.")
			else:
				self.maintenance_label.setText("Manual maintenance · Start new cleanup when ready.")
		try:
			window = resolve_window(self.service.config, datetime.now(UTC))
			self.window_label.setText("New job preview · " + format_window(window, resolve_age_cutoff(self.service.config, window.anchor_utc)))
		except (ValueError, TypeError) as exc:
			self.window_label.setText(str(exc))
		if self.current_job_id:
			jobs = self._query("SELECT * FROM jobs WHERE id=?", (self.current_job_id,))
			if jobs:
				job = jobs[0]
				self.job_status.setText(f"Job {job['id']} · {job['state']} · stage {job['stage']} · anchor {job['anchor_utc']}")
				try:
					from .domain import Window
					window = Window.from_dict(json.loads(job["window_json"]))
					frozen_config = Config.from_dict(json.loads(job["config_json"]))
					self.window_label.setText("Selected job · frozen bounds · " + format_window(window, resolve_age_cutoff(frozen_config, window.anchor_utc)))
				except (ValueError, TypeError, KeyError):
					pass
			counts = self._query("SELECT stage,state,COUNT(*) n FROM work_items WHERE job_id=? GROUP BY stage,state", (self.current_job_id,))
			total = sum(row["n"] for row in counts)
			finished = sum(row["n"] for row in counts if row["state"] in {"completed", "failed", "skipped", "cancelled"})
			if total:
				self.progress.setRange(0, total)
				self.progress.setValue(finished)
				self.progress.setFormat("%p% finalized · currently discovered stage items")
			elif jobs and jobs[0]["state"] == "completed":
				self.progress.setRange(0, 1)
				self.progress.setValue(1)
			else:
				self.progress.setRange(0, 0)
			details = " · ".join(f"{row['stage']} {row['state']}: {row['n']}" for row in counts)
			if jobs and jobs[0].get("created_at"):
				elapsed = max(0.001, (datetime.now(UTC) - datetime.fromisoformat(jobs[0]["created_at"].replace("Z", "+00:00"))).total_seconds())
				details += f"\nObserved {finished / elapsed * 60:.1f} finalized stage-items/min since job creation (paused time included)."
			retry = self._query("SELECT MIN(next_attempt_utc) next FROM work_items WHERE job_id=? AND state='retry'", (self.current_job_id,))
			if retry and retry[0].get("next"):
				details += f" Next retry: {retry[0]['next']} UTC."
			pending_ai = sum(row["n"] for row in counts if row["stage"] == "classification" and row["state"] in {"pending", "retry", "running"})
			if self.service.config.estimated_cost_per_call > 0 and pending_ai:
				batches = (pending_ai + self.service.config.ai_batch_size - 1) // self.service.config.ai_batch_size
				details += f"\nSimple batch cost estimate: {batches} remaining prompt batches × configured estimate = ${batches * self.service.config.estimated_cost_per_call:.4f}; routing/retries can differ. Actual usage is unavailable."
			self.stage_counts.setText(details)
		counts = self._query("SELECT state,action,COUNT(*) n FROM actions WHERE undo_of IS NULL GROUP BY state,action")
		self.plan_summary.setText("Proposals / receipts · " + " · ".join(f"{r['action']} {r['state']}: {r['n']}" for r in counts))
		metrics = self._query("SELECT action,COUNT(DISTINCT CASE WHEN group_id='' THEN id ELSE group_id END) n FROM feedback WHERE source='manual' GROUP BY action")
		self.learning_summary.setText("Reviewed independent events · " + " · ".join(f"{r['action']}: {r['n']}" for r in metrics) +
			". These counts measure explicit feedback; automatic decisions are not accuracy successes. Inferred archive rules require enablement.")
		self._resume_due_retry()

	def _resume_due_retry(self):
		"""Resume durable retry work when its deadline arrives, never paused jobs."""
		if self.tasks or self._running_job or self._closing:
			return False
		due = self._query("SELECT j.id FROM jobs j WHERE j.state IN ('waiting','retry') AND EXISTS "
			"(SELECT 1 FROM work_items w WHERE w.job_id=j.id AND w.state='retry' "
			"AND (w.next_attempt_utc IS NULL OR julianday(w.next_attempt_utc)<=julianday(?))) "
			"ORDER BY CASE WHEN j.id=? THEN 0 ELSE 1 END,j.created_at LIMIT 1",
			(datetime.now(UTC).isoformat(), self.current_job_id))
		if not due:
			return False
		self.current_job_id = due[0]["id"]
		self.resume_job(self.current_job_id)
		return True

	def _submit(self, title, operation, callback=None, *, pool=None):
		if self._closing:
			return
		if not self.tasks and not self._running_job:
			self.stop_event.clear()
			self._requested_control_state = None
		task_id = uuid.uuid4().hex
		task = ServiceTask(task_id, self.service, operation)
		task.signals.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
		task.signals.result.connect(self._on_result, Qt.ConnectionType.QueuedConnection)
		task.signals.error.connect(self._on_error, Qt.ConnectionType.QueuedConnection)
		task.signals.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
		self.tasks[task_id] = task
		self.callbacks[task_id] = callback
		self.statusBar().showMessage(title)
		(self.pool if pool is None else pool).start(task)
		return task_id

	@pyqtSlot(str, str)
	def _on_progress(self, task_id, text):
		if self._closing:
			return
		self.statusBar().showMessage(text)
		self.job_status.setText(text)
		if text.startswith("Started job "):
			self.current_job_id = text.removeprefix("Started job ").strip()
			self._filter_review()
			self._refresh_plan_groups()

	@pyqtSlot(str, object)
	def _on_result(self, task_id, result):
		callback = self.callbacks.pop(task_id, None)
		if self._closing:
			return
		if hasattr(self.service, "refresh_connection") and self.service.refresh_connection():
			self._connect_reader()
			old_editor = self.settings_editor
			index = self.tabs.indexOf(old_editor)
			self.tabs.removeTab(index)
			self.settings_editor = SettingsEditor(self.service.config, demo=bool(self.service.demo))
			self.settings_editor.saved.connect(self._save_settings)
			self.tabs.insertTab(index, self.settings_editor, "Settings")
			old_editor.deleteLater()
		if callback:
			callback(result)
		self.refresh(force=True)
		self.statusBar().showMessage("Operation completed", 6000)

	@pyqtSlot(str, str)
	def _on_error(self, task_id, summary):
		self.callbacks.pop(task_id, None)
		if self._closing:
			return
		self.statusBar().showMessage(summary)
		self.job_status.setText(summary)
		self.refresh(force=True)

	@pyqtSlot(str)
	def _on_finished(self, task_id):
		finished_job = task_id == self._job_task_id
		self.tasks.pop(task_id, None)
		self.callbacks.pop(task_id, None)
		if finished_job or self._job_task_id not in self.tasks:
			self._job_task_id = None
		self._running_job = self._job_task_id is not None
		if finished_job and not self._closing and self._requested_control_state and self.current_job_id:
			# A click can arrive after JobRunner's final stop check but before its
			# finished signal. Persist that last control request asynchronously too.
			job_id, state = self.current_job_id, self._requested_control_state
			self._submit("Saving requested job control", lambda svc, p: svc.control_job(job_id, state))
		if not self.tasks:
			if not self._closing:
				# Pause/stop is durable in the job row. Its in-memory cancellation
				# token must not cancel later independent review or preview actions.
				self.stop_event.clear()
				self._requested_control_state = None
			self.refresh(force=True)
		if self._closing and not self.tasks:
			self.close()

	def start_job(self):
		if self._running_job:
			self.statusBar().showMessage("A job is already running. Pause or stop it before starting another.")
			return
		self.stop_event.clear()
		self._requested_control_state = None
		self._running_job = True
		self.progress.setRange(0, 0)
		def operation(svc, progress):
			job_id = svc.new_job()
			progress(f"Started job {job_id}")
			svc.run_job(job_id, progress=progress, should_stop=lambda: self._requested_control_state or self.stop_event.is_set())
			return job_id
		self._job_task_id = self._submit("Discovering eligible inbox messages", operation, self._job_complete)

	def _job_complete(self, job_id):
		self.current_job_id = job_id
		self._filter_review()
		self._refresh_plan_groups()

	def _select_job(self, index):
		row = self.models["jobs"].record(index.row())
		self.current_job_id = row.get("id")
		self._filter_review()
		self._refresh_plan_groups()
		self.refresh()

	def resume_job(self, job_id=None):
		if self._running_job:
			return
		ids = self._selected_ids("jobs")
		job_id = job_id or (ids[0] if ids else self.current_job_id)
		if not job_id:
			self.statusBar().showMessage("Select a saved job to resume.")
			return
		self.current_job_id = job_id
		self.stop_event.clear()
		self._requested_control_state = None
		self._running_job = True
		self.progress.setRange(0, 0)
		def operation(svc, progress):
			svc.control_job(job_id, "pending")
			svc.run_job(job_id, progress=progress, should_stop=lambda: self._requested_control_state or self.stop_event.is_set())
			return job_id
		self._job_task_id = self._submit("Resuming frozen job", operation, self._job_complete)

	def _control(self, state):
		self._requested_control_state = state
		self.stop_event.set()
		ids = self._selected_ids("jobs")
		job_id = self.current_job_id or (ids[0] if ids else None)
		if job_id and self._job_task_id not in self.tasks:
			# This is still asynchronous: an external connection may hold a write
			# transaction, and a preview/label worker cannot consume JobRunner's
			# cooperative stop signal for a waiting or already-finished job.
			self._submit("Saving requested job control", lambda svc, p: svc.control_job(job_id, state))
		self.job_status.setText(f"{state.capitalize()} requested. Current AI/provider call may finish; remote cancellation is not guaranteed.")

	def _configure_maintenance(self, checked=None):
		if not hasattr(self, "maintenance_timer"):
			return
		enabled = self.in_app_maintenance.isChecked()
		self.settings.setValue("maintenance/enabled", enabled)
		self.maintenance_timer.stop()
		if enabled:
			self.maintenance_timer.start(max(1, self.service.config.maintenance_interval_minutes) * 60_000)

	def _maintenance_tick(self):
		if self._closing or self.tasks or (not self.service.demo and self.service.account_id in {"", "unconnected", None}):
			return
		if self._resume_due_retry():
			return
		active = self._query("SELECT COUNT(*) n FROM jobs WHERE state IN ('pending','running','paused','waiting','retry')")
		if not active or not active[0]["n"]:
			self.start_job()

	def _filter_review(self):
		if "review" not in self.models:
			return
		conditions, params = ["in_inbox=1", "NOT EXISTS (SELECT 1 FROM json_each(labels_json) WHERE value IN ('SPAM','TRASH','DRAFT'))"], []
		try:
			window = resolve_window(self.service.config, datetime.now(UTC))
			conditions.append("internal_ms < ?")
			params.append(window.latest_ms)
			if window.earliest_ms is not None:
				conditions.append("internal_ms >= ?")
				params.append(window.earliest_ms)
			if self.current_job_id:
				jobs = self._query("SELECT window_json FROM jobs WHERE id=?", (self.current_job_id,))
				if jobs:
					from .domain import Window
					frozen = Window.from_dict(json.loads(jobs[0]["window_json"]))
					conditions.append("internal_ms < ?")
					params.append(frozen.latest_ms)
					if frozen.earliest_ms is not None:
						conditions.append("internal_ms >= ?")
						params.append(frozen.earliest_ms)
		except (ValueError, TypeError, KeyError):
			conditions.append("0")
		text = self.review_search.text().strip()
		if text:
			conditions.append("(sender LIKE ? OR subject LIKE ? OR id LIKE ?)")
			params.extend([f"%{text}%"] * 3)
		choice = self.review_decision.currentIndex()
		if choice in {1, 2, 3}:
			conditions.append("decision=?")
			params.append({1: "review", 2: "archive", 3: "keep"}[choice])
		elif choice == 4:
			conditions.append("defer_until_utc IS NOT NULL")
		group = getattr(self, "_group_filter", None)
		if group:
			key, value = group
			conditions.append(f'"{key}"=?')
			params.append(value)
		self.models["review"].set_filter(" AND ".join(conditions), tuple(params))
		self.settings.setValue("review/search", self.review_search.text())

	def _group_changed(self):
		self._group_filter = None
		mode = self.group_by.currentIndex()
		self.group_table.setVisible(mode != 0)
		if mode:
			key = {1: "sender", 2: "category", 3: "thread_id", 4: "list_id"}[mode]
			expression = "COALESCE(json_extract(headers_json,'$.List-ID'), json_extract(headers_json,'$.list-id'), '(no list)')" if key == "list_id" else key
			query = f"SELECT {expression} AS id, {expression} AS group_name, COUNT(*) AS count, MIN(internal_ms) AS oldest, MAX(internal_ms) AS newest, COUNT(DISTINCT decision) AS outcomes FROM messages WHERE {self._group_scope_sql()} GROUP BY {expression}"
			_, columns, id_column = self.model_specs["groups"]
			self.model_specs["groups"] = query, columns, id_column
			self._replace_model("groups")
		self._filter_review()

	def _group_scope_sql(self):
		try:
			window = resolve_window(self.service.config, datetime.now(UTC))
			parts = ["in_inbox=1", f"internal_ms < {int(window.latest_ms)}",
				"NOT EXISTS (SELECT 1 FROM json_each(labels_json) WHERE value IN ('SPAM','TRASH','DRAFT'))"]
			if window.earliest_ms is not None:
				parts.append(f"internal_ms >= {int(window.earliest_ms)}")
			if self.current_job_id:
				jobs = self._query("SELECT window_json FROM jobs WHERE id=?", (self.current_job_id,))
				if jobs:
					from .domain import Window
					frozen = Window.from_dict(json.loads(jobs[0]["window_json"]))
					parts.append(f"internal_ms < {int(frozen.latest_ms)}")
					if frozen.earliest_ms is not None:
						parts.append(f"internal_ms >= {int(frozen.earliest_ms)}")
			return " AND ".join(parts)
		except (ValueError, TypeError, KeyError):
			return "0"

	def _select_group(self, index):
		row = self.models["groups"].record(index.row())
		mode = self.group_by.currentIndex()
		key = {1: "sender", 2: "category", 3: "thread_id", 4: "list_id"}.get(mode)
		if not key:
			return
		self._group_filter = key, row["id"]
		self._filter_review()
		self.group_hint.setText(f"Group {row['group_name']} · {row['count']} inbox messages · {self._date(row['oldest'])} to {self._date(row['newest'])} · {row['outcomes']} outcomes. Select explicit eligible rows below; exceptions are preserved.")

	def _show_preview(self, index):
		row = self.models["review"].record(index.row())
		rows = self._query("SELECT body,snippet,headers_json,truncated,missing_content FROM messages WHERE id=?", (row.get("id"),))
		if not rows:
			return
		message = rows[0]
		text = message.get("body") or message.get("snippet") or "No cached text. Fetch selected text or run analysis."
		prefix = f"{row.get('sender','')}\n{row.get('subject','')}\nMessage {row.get('id','')} · {self._date(row.get('internal_ms',0))}\n"
		if message.get("truncated") or message.get("missing_content"):
			prefix += "Content is incomplete; automatic archival requires conservative review.\n"
		prefix += f"\nRecommendation: {row.get('decision','')} · {row.get('reason','') or ''}\n\n"
		self.preview.set_message(prefix + text)
		self._selection_changed()

	def _selection_changed(self, *args):
		from .domain import Message, protections
		selected = self._selected("review")
		dates = [item["internal_ms"] for item in selected if item.get("internal_ms") is not None]
		shown_protections = set()
		for item in selected:
			message = Message(id=item["id"], thread_id=item["thread_id"], internal_ms=item["internal_ms"],
				labels=set(json.loads(item.get("labels_json") or "[]")), headers=json.loads(item.get("headers_json") or "{}"))
			shown_protections.update(protections(message, self.service.config))
		self.selection_label.setText(f"Selected scope: {len(selected)} explicit message IDs" +
			(f" · {self._date(min(dates))} to {self._date(max(dates))}" if dates else "") +
			". Only items eligible under frozen and current restrictions may be queued." +
			(f" Displayed protections: {', '.join(sorted(shown_protections))}. Your direct Archive instruction overrides these displayed protections for the selected targets." if shown_protections else ""))

	def _labels(self):
		return self._query("SELECT id,name,type FROM labels ORDER BY name")

	def review_action(self, action):
		rows = self._selected("review")
		ids = [row["id"] for row in rows]
		if not ids:
			self.statusBar().showMessage("Select the explicit messages to review.")
			return
		from .domain import Message, protections
		displayed_revision = self.service.config.revision
		displayed_context_versions = {row["id"]: row.get("context_version", "") for row in rows}
		displayed_protections = {}
		for row in rows:
			message = Message(id=row["id"], thread_id=row["thread_id"], internal_ms=row["internal_ms"],
				labels=set(json.loads(row.get("labels_json") or "[]")), headers=json.loads(row.get("headers_json") or "{}"))
			displayed_protections[row["id"]] = sorted(protections(message, self.service.config))
		label_ids, defer_until = [], None
		if action in {"label", "reject_label"}:
			current = set()
			suggested = set()
			for row in rows:
				current.update(json.loads(row.get("labels_json") or "[]"))
				try:
					suggested.update(json.loads(row.get("assessment_json") or "{}").get("suggested_label_ids", []))
				except (ValueError, AttributeError):
					pass
			labels = self._labels()
			if action == "reject_label":
				labels = [label for label in labels if label["id"] in suggested]
			picker = LabelPicker(labels, selected=suggested, current=current,
				corrections=action == "reject_label", parent=self)
			if picker.exec() != QDialog.DialogCode.Accepted:
				return
			label_ids = picker.chosen_ids()
			if not label_ids:
				return
		if action == "defer":
			hours, ok = QInputDialog.getInt(self, "Defer selected messages", "Reconsider after how many hours?", 24, 1, 8760)
			if not ok:
				return
			defer_until = (datetime.now(UTC) + timedelta(hours=hours)).isoformat()
		reason = "" if self.reason.currentIndex() == 0 else self.reason.currentText().lower()
		scope = self.review_scope.currentData()
		if action == "keep" and reason == "mistaken automatic archive":
			action = "mistaken_archive"
		def operation(svc, progress):
			return svc.manual(ids, action, label_ids=label_ids, reason=reason, scope=scope, defer_until=defer_until,
				displayed_revision=displayed_revision, displayed_protections=displayed_protections,
				displayed_context_versions=displayed_context_versions, should_stop=self.stop_event.is_set)
		self._submit(f"Reviewing {len(ids)} explicit message IDs", operation,
			lambda result: setattr(self, "_last_action_ids", result or []))

	def _fetch_selected_text(self):
		ids = self._selected_ids("review")
		if ids:
			self._submit("Fetching eligible text without marking read", lambda svc, p: svc.download_bodies(ids, should_stop=self.stop_event.is_set),
				lambda result: self._show_preview(self.views["review"].currentIndex()), pool=self.preview_pool)

	def open_gmail(self):
		rows = self._selected("review")
		if not rows or self.service.demo:
			self.statusBar().showMessage("Gmail links are available for connected real mailbox messages.")
			return
		row = rows[0]
		account = quote(str(self.service.account_id), safe="")
		identifier = quote(str(row.get("thread_id") or row["id"]), safe="")
		# authuser selects the account when that account is signed into the browser.
		QDesktopServices.openUrl(QUrl(f"https://mail.google.com/mail/?authuser={account}#all/{identifier}"))

	def _filter_plan(self):
		if "plan" not in self.models:
			return
		choice = self.plan_filter.currentIndex()
		where, params = "1", ()
		if choice == 1:
			where = "state IN ('queued','pending','proposed','approved')"
		elif choice == 2:
			where = "state IN ('stale','conflict','uncertain','failed')"
		elif choice in {3, 4}:
			where, params = "action=?", ({3: "archive", 4: "label"}[choice],)
		self.models["plan"].set_filter(where, params)

	def apply_plan(self, all_approved):
		ids = None if all_approved else self._selected_ids("plan")
		if not all_approved and not ids:
			return
		snapshot = self.service.plan_snapshot(action_ids=ids)
		if not self.service.config.dry_run:
			answer = QMessageBox.question(self, "Execute this plan", f"Execute this plan of {snapshot['count']} actions?\nPlan SHA-256: {snapshot['hash']}\nOnly these reviewed message IDs and label deltas are approved.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
			if answer != QMessageBox.StandardButton.Yes:
				return
		self._submit("Executing reviewed plan" if not self.service.config.dry_run else "Simulating approved plans",
			lambda svc, p: svc.apply_actions(action_ids=ids, should_stop=self.stop_event.is_set, expected_plan_hash=snapshot["hash"]))

	def _undo_scope_changed(self, *args):
		self._describe_undo_scope(self._selected("history"))

	def _describe_undo_scope(self, rows):
		window = resolve_window(self.service.config, datetime.now(UTC))
		outside = [row for row in rows if row.get("internal_ms") is not None and not window.contains(int(row["internal_ms"]))]
		unknown = sum(row.get("internal_ms") is None for row in rows)
		identifiers = ", ".join(row["message_id"] for row in outside[:10])
		self.undo_scope.setText(f"Undo scope: {len(rows)} recorded actions on original explicit message IDs; {len(outside)} outside the current cleanup date window; {unknown} with unavailable local dates. " +
			(f"Out-of-window IDs: {identifiers}" + ("…" if len(outside) > 10 else "") + ". " if outside else "") +
			"Undo is a narrow exception for recorded deltas; newer replies are never included.")

	def undo_selected(self, table="history"):
		rows = self._selected(table)
		ids = [row["id"] for row in rows]
		if not ids:
			return
		self._describe_undo_scope(rows)
		self._submit(f"Undoing {len(ids)} recorded actions", lambda svc, p: svc.undo(ids, should_stop=self.stop_event.is_set))

	def _undo_job(self):
		rows = self._selected("history")
		job_ids = {row["job_id"] for row in rows if row.get("job_id")}
		if not job_ids:
			return
		placeholders = ",".join("?" for _ in job_ids)
		actions = self._query(f"SELECT a.id,a.message_id,m.internal_ms FROM actions a LEFT JOIN messages m ON m.id=a.message_id WHERE a.job_id IN ({placeholders}) AND a.undo_of IS NULL", tuple(job_ids))
		ids = [row["id"] for row in actions]
		self._describe_undo_scope(actions)
		self._submit(f"Undoing {len(ids)} original actions from {len(job_ids)} cleanup jobs", lambda svc, p: svc.undo(ids, should_stop=self.stop_event.is_set))

	def _filter_history(self, text):
		if "history" in self.models:
			self.models["history"].set_filter("message_id LIKE ? OR job_id LIKE ? OR reason LIKE ?", (f"%{text}%",) * 3)

	def _filter_errors(self, text):
		if "errors" in self.models:
			self.models["errors"].set_filter("stage LIKE ? OR kind LIKE ? OR state LIKE ? OR summary LIKE ?", (f"%{text}%",) * 4)

	def _new_rule(self):
		dialog = RuleDialog(parent=self)
		dialog.previewRequested.connect(lambda conditions: self._preview_rule(conditions, dialog))
		rows = self._selected("review") if "review" in self.models else []
		if rows:
			conditions = {"sender": rows[0].get("sender", ""), "category": rows[0].get("category", "other")}
			dialog.conditions.setPlainText(json.dumps(conditions, indent=2))
			dialog.summary.setText(f"Current sample: {len(rows)} selected messages. Conditions apply to future eligible matches; protections and uncertain attention still take precedence.")
		if dialog.exec() == QDialog.DialogCode.Accepted:
			self._submit("Saving explicit rule", lambda svc, p: svc.learning.save_rule(dialog.rule))

	def _edit_rule(self):
		ids = self._selected_ids("rules")
		if not ids:
			return
		rows = self._query("SELECT * FROM rules WHERE id=?", (ids[0],))
		if not rows:
			return
		rule = rows[0]
		rule["conditions"] = json.loads(rule.pop("conditions_json"))
		rule["label_ids"] = json.loads(rule.pop("label_ids_json", "[]"))
		dialog = RuleDialog(rule, self)
		dialog.previewRequested.connect(lambda conditions: self._preview_rule(conditions, dialog))
		if dialog.exec() == QDialog.DialogCode.Accepted:
			self._submit("Updating rule and affected recommendations", lambda svc, p: svc.learning.save_rule(dialog.rule))

	def _preview_rule(self, conditions, dialog):
		def operation(svc, progress):
			from types import SimpleNamespace
			from .domain import Message, eligible, protections, rule_matches
			window = resolve_window(svc.config, datetime.now(UTC))
			cursor = svc.store.conn.execute("SELECT id,thread_id,internal_ms,labels_json,headers_json,category,decision FROM messages WHERE in_inbox=1 AND internal_ms < ?", (window.latest_ms,))
			count, protected, exceptions, samples = 0, 0, 0, []
			while True:
				if self.stop_event.is_set():
					return "Rule preview stopped."
				batch = cursor.fetchmany(128)
				if not batch:
					break
				for row in batch:
					message = Message(id=row["id"], thread_id=row["thread_id"], internal_ms=row["internal_ms"],
						labels=set(json.loads(row["labels_json"])), headers=json.loads(row["headers_json"]))
					if not eligible(message, window) or not rule_matches({"enabled": True, "conditions": conditions}, message, SimpleNamespace(category=row["category"])):
						continue
					count += 1
					protected += bool(protections(message, svc.config))
					exceptions += row["decision"] in {"review", "keep", "defer"}
					if len(samples) < 5:
						samples.append(f"{message.id}: {message.subject[:80]}")
			return (f"{count} current eligible matches; {protected} protected; {exceptions} currently kept/review/deferred exceptions. "
				"Rules never override unresolved attention or protections. Sample IDs: " + "; ".join(samples))
		dialog.summary.setText("Calculating matches in the worker…")
		def done(result):
			try:
				dialog.summary.setText(result)
			except RuntimeError:
				pass  # Dialog may have closed while its preview was queued.
		self._submit("Previewing rule matches without loading message bodies", operation, done, pool=self.preview_pool)

	def _toggle_rules(self, enabled):
		ids = self._selected_ids("rules")
		if not ids:
			return
		def operation(svc, progress):
			for rule_id in ids:
				svc.learning.set_rule_enabled(rule_id, enabled)
			svc.replan(should_stop=self.stop_event.is_set)
		self._submit("Changing selected rule activation", operation)

	def _export_rules(self):
		path, _ = QFileDialog.getSaveFileName(self, "Export reusable rules", "gmail-clean-rules.json", "JSON (*.json)")
		if path:
			self._submit("Exporting rules", lambda svc, p: svc.learning.export_rules(path))

	def _import_rules(self):
		path, _ = QFileDialog.getOpenFileName(self, "Import reusable rules", "", "JSON (*.json)")
		if path:
			self._submit("Importing rules", lambda svc, p: svc.learning.import_rules(path))

	def _forget_examples(self):
		ids = self._selected_ids("examples")
		if not ids:
			return
		def operation(svc, progress):
			for example_id in ids:
				svc.learning.forget(example_id)
			svc.replan(should_stop=self.stop_event.is_set)
		self._submit("Forgetting examples and rebuilding derived learning", operation)

	def _reset_learning(self):
		text, ok = QInputDialog.getText(self, "Reset learning", "Sender address to reset; blank resets all learned examples and statistics. Explicit current-item decisions and undo history are retained.")
		if ok:
			def operation(svc, progress):
				svc.learning.reset(sender=text.strip() or None)
				svc.replan(should_stop=self.stop_event.is_set)
			self._submit("Resetting learning", operation)

	def _save_settings(self, config):
		def operation(svc, progress):
			previous_revision = svc.config.revision
			svc.update_config(config)
			if previous_revision != config.revision:
				svc.replan(should_stop=self.stop_event.is_set)
			return config
		def done(result):
			self.service.config = result
			self.settings_editor.config = result
			save_preferences(result, self.settings)
			self._setup_shortcuts()
			self._configure_maintenance()
			self._filter_review()
			self._refresh_plan_groups()
		self._submit("Saving preferences and invalidating affected proposals", operation, done)

	def _connect_account(self):
		if self.service.demo:
			self.statusBar().showMessage("Offline demo cannot connect to or modify Gmail. Launch without --demo to connect.")
			return
		path, _ = QFileDialog.getOpenFileName(self, "Desktop OAuth client JSON", "", "JSON (*.json)")
		if path:
			self._submit("Opening installed-desktop OAuth consent", lambda svc, p: svc.connect(path))

	def _disconnect_account(self):
		if not self.service.demo:
			self._submit("Disconnecting and removing protected local tokens", lambda svc, p: svc.disconnect())

	def _backup(self):
		path, _ = QFileDialog.getSaveFileName(self, "Consistent database backup", "gmail-clean-backup.sqlite", "SQLite (*.sqlite)")
		if path:
			self._submit("Backing up local state", lambda svc, p: svc.backup(path))

	def _setup_shortcuts(self):
		for shortcut in getattr(self, "_shortcuts", []):
			shortcut.setEnabled(False)
			shortcut.deleteLater()
		self._shortcuts = []
		mapping = dict(self.DEFAULT_SHORTCUTS)
		mapping.update(getattr(self.service.config, "shortcuts", {}) or {})
		for action, button in self.review_buttons.items():
			label = {"archive": "Archive", "keep": "Keep / needs attention", "defer": "Defer…", "label": "Add labels…", "open": "Open in Gmail"}[action]
			button.setText(label + (f" [{mapping[action]}]" if mapping.get(action) else ""))
		for action, sequence in mapping.items():
			if action not in self.DEFAULT_SHORTCUTS or not sequence:
				continue
			shortcut = QShortcut(QKeySequence(str(sequence)), self)
			shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
			shortcut.activated.connect(lambda action=action: self._shortcut_action(action))
			self._shortcuts.append(shortcut)
		self._focus_changed(None, QApplication.focusWidget())

	def _focus_changed(self, old, current):
		editor = isinstance(current, (QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit))
		for shortcut in self._shortcuts:
			shortcut.setEnabled(not editor)

	def _shortcut_action(self, action):
		focus = QApplication.focusWidget()
		if isinstance(focus, (QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit)):
			return
		if action == "undo":
			if self._last_action_ids:
				ids = list(self._last_action_ids)
				self._submit("Undoing last review action", lambda svc, p: svc.undo(ids, should_stop=self.stop_event.is_set))
			return
		if self.tabs.currentIndex() != 1:
			return
		if action == "open":
			self.open_gmail()
		else:
			self.review_action(action)

	def closeEvent(self, event):
		if self._resources_closed:
			event.accept()
			return
		if self.tasks:
			if not self._closing:
				self._closing = True
				self._requested_control_state = "stopped"
				self.stop_event.set()
				self.refresh_timer.stop()
				self.maintenance_timer.stop()
				if hasattr(self.service, "abort_workers"):
					self.service.abort_workers()
				self.pool.clear()
				self.preview_pool.clear()
				self._close_deadline.start(3000)
				self.statusBar().showMessage("Stopping local workers and closing. An interrupted remote request may still complete or be billed.")
			event.ignore()
			return
		self._closing = True
		self._resources_closed = True
		self._close_deadline.stop()
		self.refresh_timer.stop()
		self.maintenance_timer.stop()
		self.settings.setValue("window/geometry", self.saveGeometry())
		self.settings.setValue("review/splitter", self.review_splitter.saveState())
		for key, view in self.views.items():
			self.settings.setValue(f"tables/{key}/header", view.horizontalHeader().saveState())
		self.settings.sync()
		for model in self.models.values():
			model.close()
		if self._reader:
			self._reader.close()
		self.service.close()
		event.accept()

	def _force_close(self):
		"""Bound the UI close without deleting a pool or Store owned by a worker.

		The process launcher can detect ``shutdown_incomplete`` and record an
		abrupt local shutdown. Gmail effects remain uncertain until reconciliation;
		no Qt worker thread is forcibly terminated or claimed to cancel a request.
		"""
		if self._resources_closed:
			return
		self.shutdown_incomplete = bool(self.tasks)
		if self.tasks:
			for task in self.tasks.values():
				for signal in (task.signals.progress, task.signals.result, task.signals.error, task.signals.finished):
					try:
						signal.disconnect()
					except TypeError:
						pass  # A task can finish before all UI receivers are detached.
			_DrainingWork((self.pool, self.preview_pool), self.tasks)
			self.tasks = {}
			self.callbacks.clear()
			self._running_job = False
		self.close()
