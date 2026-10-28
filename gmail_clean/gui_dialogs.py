"""Explicit label and rule review dialogs."""
from __future__ import annotations

from datetime import timezone
import json

from PyQt6.QtCore import (Qt, pyqtSignal)
from PyQt6.QtWidgets import (QCheckBox,
	QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
	QPlainTextEdit, QPushButton,
	QVBoxLayout)



UTC = timezone.utc

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
