import base64

from gmail_clean.mime import html_to_text, parse_body, sanitize_html


def part(mime, value, charset="utf-8", **extra):
	return {"mimeType": mime, "headers": [{"name": "Content-Type", "value": f"{mime}; charset={charset}"}],
		"body": {"data": base64.urlsafe_b64encode(value.encode(charset)).decode().rstrip("=")}, **extra}


def test_alternative_prefers_plain_and_never_fetches_binary_attachments():
	calls = []
	payload = {"mimeType": "multipart/mixed", "parts": [
		{"mimeType": "multipart/alternative", "parts": [
			part("text/html", "<p>HTML alternative<img src='https://tracker.invalid'></p>"),
			part("text/plain", "Plain useful alternative")
		]},
		{"mimeType": "application/pdf", "filename": "invoice.pdf", "body": {"attachmentId": "binary", "size": 1200}},
	]}
	parsed = parse_body(payload, lambda attachment_id: calls.append(attachment_id))
	assert parsed.text == "Plain useful alternative"
	assert not parsed.missing
	assert calls == []
	assert parsed.attachments == [{"name": "invoice.pdf", "type": "application/pdf", "size": 1200}]


def test_nested_inline_text_attachment_charset_and_truncation():
	calls = []
	data = base64.urlsafe_b64encode("Crème brûlée".encode("iso-8859-1")).decode()
	payload = {"mimeType": "multipart/mixed", "parts": [{"mimeType": "multipart/related", "parts": [
		{"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
			"body": {"attachmentId": "inline", "size": 12}}
	]}]}
	def fetch(value):
		calls.append(value)
		return {"data": data}
	parsed = parse_body(payload, fetch, max_chars=5)
	assert parsed.text == "Crème"
	assert parsed.truncated
	assert calls == ["inline"]


def test_html_is_inert_and_tracks_missing_or_invalid_content():
	value = "<html><head><style>secret-css</style></head><body><script>alert('bad')</script><p>Hello<img src='https://tracker.invalid/pixel'></p><iframe>hidden</iframe><a href='javascript:bad()'>there</a></body></html>"
	assert html_to_text(value) == "Hello\nthere"
	safe = sanitize_html(value)
	assert "script" not in safe and "https:" not in safe and "javascript:" not in safe
	assert safe == "<p>Hello<br>there</p>"
	assert parse_body({"mimeType": "text/plain", "body": {"data": "!!!"}}).missing
	assert parse_body({"mimeType": "text/plain", "body": {"size": 0}}).missing
	assert parse_body({"mimeType": "text/plain", "body": {"attachmentId": "missing", "size": 5}}).missing


def test_attached_text_is_not_analyzed_and_empty_plain_falls_back_to_html():
	payload = {"mimeType": "multipart/mixed", "parts": [
		part("text/plain", "confidential attachment", filename="private.txt"),
		{"mimeType": "multipart/alternative", "parts": [part("text/plain", ""), part("text/html", "<p>Visible body</p>")]},
	]}
	parsed = parse_body(payload)
	assert parsed.text == "Visible body"
	assert "confidential" not in parsed.text


def test_oversized_and_unknown_charset_never_silently_become_complete():
	parsed = parse_body({"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=unknown-wrong"}],
		"body": {"data": "SGVsbG8"}})
	assert parsed.text == "Hello" and parsed.missing
	calls = []
	parsed = parse_body({"mimeType": "text/plain", "body": {"attachmentId": "large", "size": 2_000_000}},
		lambda value: calls.append(value))
	assert parsed.truncated and parsed.missing
	assert calls == []
