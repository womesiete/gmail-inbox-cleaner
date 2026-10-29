"""Bounded, inert email text extraction. No attachment execution or URL retrieval."""
from __future__ import annotations

import base64
from bisect import bisect_left
import re
from dataclasses import dataclass, field
from email.message import Message as HeaderMessage
from html import escape
from html.parser import HTMLParser
from typing import Callable

MAX_TEXT_CHARS = 100_000
MAX_PART_BYTES = 1_000_000


class _TextParser(HTMLParser):
	"""Extract visible text while remembering omitted, potentially material content."""
	BLOCKED = {"script", "style", "iframe", "object", "noscript", "svg", "math", "head", "template", "title"}
	VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.output: list[str] = []
		self.stack: list[tuple[str, bool, bool]] = []
		self._blocked_depth = 0
		self._hidden_depth = 0
		self.hidden_content = False

	@property
	def blocked(self) -> bool:
		return self._blocked_depth > 0

	@staticmethod
	def _css(value: str) -> str:
		# Resolve simple CSS escapes/comments before testing visibility declarations.
		value = re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL)
		def unescape(match):
			if match[1]:
				codepoint = int(match[1], 16)
				return chr(codepoint) if 0 < codepoint <= 0x10FFFF else "\ufffd"
			return match[2] or ""
		value = re.sub(r"\\(?:([0-9a-fA-F]{1,6})\s?|(.))", unescape, value)
		return re.sub(r"!\s*important\b", "", value, flags=re.IGNORECASE).casefold()

	@classmethod
	def _hidden(cls, attrs: list[tuple[str, str | None]]) -> bool:
		values = {name.casefold(): value or "" for name, value in attrs}
		if "hidden" in values or values.get("aria-hidden", "").strip().casefold() == "true":
			return True
		style = dict(piece.split(":", 1) for piece in cls._css(values.get("style", "")).split(";") if ":" in piece)
		style = {key.strip(): value.strip() for key, value in style.items()}
		return (style.get("display") == "none" or style.get("visibility") == "hidden"
			or bool(re.fullmatch(r"[+-]?(?:0+(?:\.0*)?|\.0+)(?:e[+-]?\d+)?(?:px|pt|pc|in|cm|mm|q|em|rem|%|ex|ch|vw|vh|vmin|vmax)?", style.get("font-size", ""))))

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		hidden = tag in {"template", "title"} or self._hidden(attrs)
		if tag not in self.VOID:
			blocked = hidden or tag in self.BLOCKED
			self.stack.append((tag, blocked, hidden))
			self._blocked_depth += int(blocked)
			self._hidden_depth += int(hidden)
		if not self.blocked and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "blockquote"}:
			self.output.append("\n")

	def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		self.handle_starttag(tag, attrs)
		self.handle_endtag(tag)

	def handle_endtag(self, tag: str) -> None:
		for index in range(len(self.stack) - 1, -1, -1):
			if self.stack[index][0] == tag:
				closed = self.stack[index:]
				self._blocked_depth -= sum(blocked for _, blocked, _ in closed)
				self._hidden_depth -= sum(hidden for _, _, hidden in closed)
				del self.stack[index:]
				break
		if not self.blocked and tag in {"p", "div", "li", "tr", "blockquote"}:
			self.output.append("\n")

	def handle_data(self, data: str) -> None:
		if self.blocked:
			# Stylesheet selectors are not evaluated. Flag hidden-style declarations
			# conservatively so class-based hidden text never reaches a shared prompt.
			if any(tag == "style" for tag, _, _ in self.stack) and any(self._hidden([("style", declaration)]) for declaration in re.split(r"[{}]", data)):
				self.hidden_content = True
			if data.strip() and self._hidden_depth:
				self.hidden_content = True
		else:
			self.output.append(data)


def _parse_html(value: str) -> tuple[_TextParser, bool]:
	"""Recover visible body boundaries after unclosed non-executable blocks.

	Only head/object/noscript are repaired. Script/style remain inert even when
	malformed; guessed recovery is always incomplete evidence requiring review.
	"""
	pending: dict[str, list[re.Match[str]]] = {tag: [] for tag in ("head", "object", "noscript")}
	for match in re.finditer(r"<\s*(/?)\s*(head|object|noscript)\b[^>]*>", value, re.IGNORECASE):
		tag = match[2].casefold()
		if match[1]:
			if pending[tag]:
				pending[tag].pop()
		elif not match[0].rstrip().endswith("/>"):
			pending[tag].append(match)
	boundaries = [match.start() for match in re.finditer(r"<\s*(?:body|p|div|section|article|main|table|h[1-6]|ul|ol|blockquote)\b", value, re.IGNORECASE)]
	insertions: dict[int, list[str]] = {}
	for opening in sorted((match for matches in pending.values() for match in matches), key=lambda item: item.start(), reverse=True):
		index = bisect_left(boundaries, opening.end())
		if index < len(boundaries):
			insertions.setdefault(boundaries[index], []).append(f"</{opening[2]}>")
	if insertions:
		pieces = []
		previous = 0
		for position, closings in sorted(insertions.items()):
			pieces.append(value[previous:position])
			pieces.extend(closings)
			previous = position
		pieces.append(value[previous:])
		value = "".join(pieces)
	parser = _TextParser()
	parser.feed(value)
	parser.close()
	return parser, bool(insertions)


def _plain_stub(value: str) -> bool:
	"""Short HTML/browser-view notices are not substantive plain alternatives."""
	return len(value) <= 240 and bool(re.search(
		r"\b(?:view|read|open)\b.{0,50}\b(?:browser|online)\b"
		r"|\b(?:html|rich.text)\b.{0,50}\b(?:version|only|support|client)\b"
		r"|\b(?:does not|doesn't|cannot)\b.{0,30}\b(?:support|display)\b.{0,30}\bhtml\b", value, re.IGNORECASE))


def normalize_text(value: str) -> str:
	value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
	value = value.translate(str.maketrans("", "", "\u200b\u200c\u200d\ufeff\u2060\u00ad"))
	value = value.replace("\u00a0", " ").replace("\u2007", " ").replace("\u202f", " ")
	value = re.sub(r"[ \t\f\v]+", " ", value)
	return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", value).strip()


def html_to_text(value: str) -> str:
	parser, _ = _parse_html(value)
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
	content_flags: list[str] = field(default_factory=list)


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
			# Useful plain text still avoids HTML downloads. A browser-view stub
			# may fall back to materially longer HTML, without following its URL.
			ordered = sorted(children, key=lambda p: str(p.get("mimeType", "")) != "text/plain")
			fallback = ""
			for child in ordered:
				text = walk(child, depth + 1)
				if not text.strip():
					continue
				if not fallback:
					fallback = text
					if str(child.get("mimeType", "")).casefold() != "text/plain" or not _plain_stub(text):
						return text
				elif str(child.get("mimeType", "")).casefold() == "text/html" and len(text) >= max(80, len(fallback) * 2):
					return text
			return fallback
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
			parser, recovered = _parse_html(text)
			result.truncated = result.truncated or recovered or bool(parser.blocked)
			if parser.hidden_content and "hidden_content" not in result.content_flags:
				result.content_flags.append("hidden_content")
			return normalize_text("".join(parser.output))
		return normalize_text(text)

	result.text = normalize_text(walk(payload))
	if len(result.text) > max_chars:
		result.text = result.text[:max_chars]
		result.truncated = True
	if not result.text:
		result.missing = True
	return result
