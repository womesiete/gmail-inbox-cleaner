"""Sanitized optional HTML previews with resource loading blocked."""
from __future__ import annotations

from datetime import timezone
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment
from PyQt6.QtCore import (QByteArray, QUrl, pyqtSlot)
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QTextBrowser)



UTC = timezone.utc

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
