"""Preferences, typed settings controls, and application instance locking."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from PyQt6.QtCore import (QLockFile, QSettings,
	pyqtSignal)
from PyQt6.QtWidgets import (QCheckBox,
	QComboBox, QDoubleSpinBox,
	QFormLayout, QGroupBox, QHBoxLayout,
	QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
	QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from .domain import Config, resolve_age_cutoff, resolve_window
from .ai import AIError, module_trust_details


UTC = timezone.utc

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


class SettingsEditor(QWidget):
	"""Typed controls for the complete Config schema, persisted using QSettings."""
	saved = pyqtSignal(object)

	GROUPS = [
		("Examination window", ["timezone", "earliest_date", "latest_mode", "latest_days", "latest_date"]),
		("Archival policy", ["age_enabled", "age_months", "age_days", "category_age_overrides", "archive_threshold", "keep_threshold", "age_archive_max_score"]),
		("Protection and labels", ["protect_starred", "protected_senders", "protected_domains", "protected_label_ids", "label_meanings"]),
		("Execution and learning", ["dry_run", "auto_execute", "auto_label", "learning_enabled"]),
		("External AI", ["ai_module", "ai_model", "ai_use_call_api_result", "ai_batch_size", "ai_timeout_seconds", "optional_model_routes", "estimated_cost_per_call", "max_ai_calls_per_job", "max_ai_calls_per_day", "max_ai_cost_per_job", "max_ai_cost_per_day"]),
		("Content sharing and context", ["excluded_senders", "excluded_domains", "excluded_label_ids", "sent_context_enabled", "archived_context_enabled", "download_all_bodies", "body_limit"]),
		("Jobs and local data", ["retrieval_batch_size", "quota_units_per_minute", "metadata_cache_ttl_seconds", "execution_batch_size", "max_retries", "retry_base_seconds", "maintenance_interval_minutes", "reconciliation_interval_hours", "retention_days", "log_level", "shortcuts"]),
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
		"max_ai_calls_per_job": "AI calls per job cap · 0 = unlimited",
		"max_ai_calls_per_day": "AI calls per UTC day cap · 0 = unlimited",
		"max_ai_cost_per_job": "Estimated AI cost per job cap · 0 = unlimited",
		"max_ai_cost_per_day": "Estimated AI cost per UTC day cap · 0 = unlimited",
		"estimated_cost_per_call": "Estimated cost per attempted AI call",
		"quota_units_per_minute": "Gmail quota budget · units per minute",
		"metadata_cache_ttl_seconds": "Metadata cache validity · seconds (0 = disabled)",
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
		else:
			from .auth import TokenStore
			self.credential_backend = QLabel(f"Protected OAuth storage: {TokenStore.backend_name()}", wordWrap=True)
			outer.addWidget(self.credential_backend)
		self.resolved = QLabel("", wordWrap=True)
		outer.addWidget(self.resolved)
		scroll = QScrollArea()
		scroll.setWidgetResizable(True)
		container = QWidget()
		groups_layout = QVBoxLayout(container)
		values = config.to_dict()
		included = set()
		groups = list(self.GROUPS)
		remaining = [key for key in values if key not in {k for _, keys in groups for k in keys} and key not in {"extras", "ai_module_sha256"}]
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
		self.batch_warning = QLabel("Sharing multiple messages in one AI prompt can let one message influence another. Keep this at 1 for strongest isolation; flagged content is never shared.", wordWrap=True)
		outer.insertWidget(2, self.batch_warning)
		batch_control = self.controls["ai_batch_size"][0]
		batch_control.valueChanged.connect(self._update_batch_warning)
		self._update_batch_warning(batch_control.value())
		self._preview()

	def _update_batch_warning(self, value):
		self.batch_warning.setVisible(value > 1)

	def _control(self, key, value):
		if isinstance(value, bool):
			widget = QCheckBox()
			widget.setChecked(value)
		elif isinstance(value, int):
			widget = QSpinBox()
			limits = {"ai_batch_size": (1, 10), "retrieval_batch_size": (1, 50),
				"execution_batch_size": (1, 500), "quota_units_per_minute": (60, 1_000_000),
				"metadata_cache_ttl_seconds": (0, 300), "archive_threshold": (0, 100),
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
			if config.ai_module and not self.demo and config.ai_module != "gmail_clean.demo_ai":
				info = module_trust_details(config.ai_module)
				if config.ai_module != self.config.ai_module or info["sha256"] != self.config.ai_module_sha256:
					warning = "\n".join(info["warnings"])
					if QMessageBox.question(self, "Trust external Python code?",
						f"This provider runs as Python code with your user account's file and network access.\nSource: {info['path']}\nSHA-256: {info['sha256']}\n{warning}\nApprove this exact source after reviewing it?",
						QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
						QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
						return
					config.ai_module_sha256 = info["sha256"]
			self.saved.emit(config)
		except (AIError, ValueError, TypeError, KeyError) as exc:
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
