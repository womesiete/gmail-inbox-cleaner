"""Bounded, inert email text extraction. No attachment execution or URL retrieval."""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from email.message import Message as HeaderMessage
from html import escape
from html.parser import HTMLParser
from typing import Callable

MAX_TEXT_CHARS = 100_000
MAX_PART_BYTES = 1_000_000


class _TextParser(HTMLParser):
	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.output: list[str] = []
		self.blocked = 0

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		if tag in {"script", "style", "iframe", "object", "noscript", "svg", "math", "head"}:
			self.blocked += 1
		if not self.blocked and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "blockquote"}:
			self.output.append("\n")

	def handle_endtag(self, tag: str) -> None:
		if tag in {"script", "style", "iframe", "object", "noscript", "svg", "math", "head"}:
			self.blocked = max(0, self.blocked - 1)
		if not self.blocked and tag in {"p", "div", "li", "tr", "blockquote"}:
			self.output.append("\n")

	def handle_data(self, data: str) -> None:
		if not self.blocked:
			self.output.append(data)


def normalize_text(value: str) -> str:
	value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
	value = value.translate(str.maketrans("", "", "\u200b\u200c\u200d\ufeff\u2060\u00ad"))
	value = value.replace("\u00a0", " ").replace("\u2007", " ").replace("\u202f", " ")
	value = re.sub(r"[ \t\f\v]+", " ", value)
	return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", value).strip()


def html_to_text(value: str) -> str:
	parser = _TextParser()
	parser.feed(value)
	parser.close()
	return normalize_text("".join(parser.output))


def sanitize_html(value: str) -> str:
	"""Return inert formatted text; all email markup/resources become plain text.

	QTextBrowser receives escaped paragraphs rather than permissive email HTML.
	This strict allowlist is intentionally just p/br, with no URLs or attributes.
	"""
	return "<p>" + escape(html_to_text(value)).replace("\n", "<br>") + "</p>"


@dataclass
class ParsedBody:
	text: str = ""
	truncated: bool = False
	missing: bool = False
	attachments: list[dict[str, str | int]] = field(default_factory=list)


def decode_base64url(data: str) -> bytes:
	# MIME transfer data can contain folded ASCII whitespace or use the standard
	# alphabet. Normalize those representations, but still reject arbitrary junk.
	cleaned = data.translate(str.maketrans("", "", " \t\r\n\f\v")).replace("+", "-").replace("/", "_")
	if (data and not cleaned) or len(cleaned) % 4 == 1:
		raise ValueError("Invalid base64url data")
	return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), altchars=b"-_", validate=True)


def _headers(part: dict) -> dict[str, str]:
	return {str(x.get("name", "")).lower(): str(x.get("value", "")) for x in part.get("headers", [])}


def parse_body(payload: dict, fetch_text_attachment: Callable[[str], dict] | None = None,
		max_chars: int = MAX_TEXT_CHARS) -> ParsedBody:
	"""Choose text alternatives, collect inline text only, and mark incompleteness.

	The callback is used solely for inline text/plain or text/html part bodies.
	Named attachments and Content-Disposition: attachment are never downloaded.
	"""
	result = ParsedBody()
	part_count = 0

	def walk(part: dict, depth: int = 0) -> str:
		nonlocal part_count
		part_count += 1
		if depth > 30 or part_count > 500:
			result.truncated = True
			return ""
		headers = _headers(part)
		mime = str(part.get("mimeType", "")).lower()
		filename = str(part.get("filename", ""))
		body = part.get("body") or {}
		if filename or headers.get("content-disposition", "").lower().startswith("attachment"):
			result.attachments.append({"name": filename, "type": mime, "size": int(body.get("size", 0))})
			return ""
		children = part.get("parts") or []
		if mime == "multipart/alternative":
			# Do not download HTML tracking alternatives when useful plain text exists.
			ordered = sorted(children, key=lambda p: str(p.get("mimeType", "")) != "text/plain")
			for child in ordered:
				text = walk(child, depth + 1)
				if text.strip():
					return text
			return ""
		if children:
			return "\n\n".join(filter(None, (walk(child, depth + 1) for child in children)))
		if mime not in {"text/plain", "text/html"}:
			return ""
		data = body.get("data")
		if data is None and body.get("attachmentId"):
			if int(body.get("size", 0)) > MAX_PART_BYTES:
				result.truncated = True
				return ""
			if fetch_text_attachment is None:
				result.missing = True
				return ""
			try:
				data = fetch_text_attachment(str(body["attachmentId"])).get("data")
			except Exception:  # noqa: BLE001 - attachment callback boundary marks unavailable content without leaking errors
				result.missing = True
				return ""
		if data is None:
			if int(body.get("size", 0)):
				result.missing = True
			return ""
		if not isinstance(data, str) or len(data) > MAX_PART_BYTES * 2:
			result.truncated = True
			return ""
		try:
			raw = decode_base64url(data)
		except (ValueError, TypeError):
			result.missing = True
			return ""
		if len(raw) > MAX_PART_BYTES:
			raw = raw[:MAX_PART_BYTES]
			result.truncated = True
		content_type = HeaderMessage()
		content_type["content-type"] = headers.get("content-type", mime)
		charset = content_type.get_content_charset() or "utf-8"
		try:
			text = raw.decode(charset, errors="strict")
		except (LookupError, UnicodeDecodeError):
			# A wrong/missing declaration does not imply missing bytes. Keep the
			# complete decoded text, preferring UTF-8 and common legacy mail encodings.
			for fallback in ("utf-8", "cp1252", "latin-1"):
				try:
					text = raw.decode(fallback, errors="strict")
					break
				except UnicodeDecodeError:
					continue
		if mime == "text/html":
			parser = _TextParser()
			parser.feed(text)
			parser.close()
			result.truncated = result.truncated or bool(parser.blocked)
			return normalize_text("".join(parser.output))
		return normalize_text(text)

	result.text = normalize_text(walk(payload))
	if len(result.text) > max_chars:
		result.text = result.text[:max_chars]
		result.truncated = True
	if not result.text:
		result.missing = True
	return result
