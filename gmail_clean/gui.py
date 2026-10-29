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

from .runtime import chunks, placeholders
from .domain import Config, resolve_age_cutoff, resolve_window
from .ai import AIError, module_trust_details
from .table_model import Column, SqlTableModel



from .gui_preferences import preferences, load_preferences, save_preferences, InstanceLockError, acquire_instance_lock, SettingsEditor, format_window
from .gui_preview import SafePreview
from .gui_workers import _DrainingWork, TaskSignals, ServiceTask
from .gui_dialogs import LabelPicker, RuleDialog
from .gui_window import MainWindow
from .gui_workers import _DRAINING_WORK
from .gui_window import UTC

__all__ = [
	'datetime',
	'timedelta',
	'timezone',
	'json',
	'Path',
	'sqlite3',
	'threading',
	'uuid',
	'quote',
	'urlsplit',
	'ZoneInfo',
	'QByteArray',
	'QLockFile',
	'QObject',
	'QRunnable',
	'QSettings',
	'Qt',
	'QThreadPool',
	'QTimer',
	'QUrl',
	'QItemSelectionModel',
	'pyqtSignal',
	'pyqtSlot',
	'QDesktopServices',
	'QKeySequence',
	'QShortcut',
	'QAbstractItemView',
	'QApplication',
	'QCheckBox',
	'QComboBox',
	'QDateTimeEdit',
	'QDialog',
	'QDialogButtonBox',
	'QDoubleSpinBox',
	'QFileDialog',
	'QFormLayout',
	'QGroupBox',
	'QHBoxLayout',
	'QInputDialog',
	'QLabel',
	'QLineEdit',
	'QListWidget',
	'QListWidgetItem',
	'QMainWindow',
	'QMessageBox',
	'QPlainTextEdit',
	'QProgressBar',
	'QPushButton',
	'QScrollArea',
	'QSpinBox',
	'QSplitter',
	'QStatusBar',
	'QTabWidget',
	'QTableView',
	'QTextBrowser',
	'QVBoxLayout',
	'QWidget',
	'chunks',
	'placeholders',
	'Config',
	'resolve_age_cutoff',
	'resolve_window',
	'AIError',
	'module_trust_details',
	'Column',
	'SqlTableModel',
	'_DrainingWork',
	'preferences',
	'load_preferences',
	'save_preferences',
	'InstanceLockError',
	'acquire_instance_lock',
	'SafePreview',
	'TaskSignals',
	'ServiceTask',
	'LabelPicker',
	'RuleDialog',
	'SettingsEditor',
	'format_window',
	'MainWindow',
	'UTC',
	'_DRAINING_WORK',
]
