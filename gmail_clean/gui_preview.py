"""Plain-text email previews with every resource-loading path blocked."""
from __future__ import annotations

from PyQt6.QtCore import QByteArray
from PyQt6.QtWidgets import QTextBrowser


class SafePreview(QTextBrowser):
	def __init__(self, parent=None):
		super().__init__(parent)
		self.setOpenLinks(False)
		self.setOpenExternalLinks(False)

	def loadResource(self, resource_type, name):
		# This includes file:, data:, and remote schemes. Email never gets a loader.
		return QByteArray()

	def set_message(self, text: str):
		# Email is already normalized by the MIME layer. Literal markup stays text.
		self.setPlainText(text)
