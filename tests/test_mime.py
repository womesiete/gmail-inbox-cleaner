import base64

import pytest

from gmail_clean.mime import decode_base64url, html_to_text, parse_body


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


def test_oversized_content_is_incomplete_and_unknown_charset_uses_lossless_fallback():
	parsed = parse_body({"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=unknown-wrong"}],
		"body": {"data": "SGVsbG8"}})
	assert parsed.text == "Hello" and not parsed.missing
	calls = []
	parsed = parse_body({"mimeType": "text/plain", "body": {"attachmentId": "large", "size": 2_000_000}},
		lambda value: calls.append(value))
	assert parsed.truncated and parsed.missing
	assert calls == []


def test_bad_charset_uses_cp1252_then_latin1_without_claiming_missing_text():
	for raw, expected in [(b"Total \x80 12", "Total € 12"), (b"data \x81 bytes", "data \x81 bytes")]:
		parsed = parse_body({"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(raw).decode()}})
		assert parsed.text == expected
		assert not parsed.missing


@pytest.mark.parametrize("tag", ["script", "style", "head"])
def test_unclosed_hidden_html_is_reported_truncated(tag):
	parsed = parse_body(part("text/html", f"<p>Visible</p><{tag}>invisible remainder"))
	assert parsed.text == "Visible"
	assert parsed.truncated


def test_zero_width_preheader_padding_is_removed_from_plain_and_html():
	value = "Useful\u200b\u200c\u200d\ufeff\u2060\u00ad" + "\u00a0" * 150 + " message"
	assert parse_body(part("text/plain", value)).text == "Useful message"
	assert parse_body(part("text/html", f"<p>{value}</p>")).text == "Useful message"


def test_folded_standard_base64_text_and_urlsafe_alphabet_decode_identically():
	# UTF-8 bytes for this text exercise the standard '+' alphabet as well as
	# folded whitespace. Binary bytes below additionally exercise '/'.
	value = "A useful receipt: \u0fff"
	encoded = base64.b64encode(value.encode()).decode().rstrip("=")
	folded = " \r\n\t" + " \r\n".join(encoded[i:i + 5] for i in range(0, len(encoded), 5)) + "\n"
	parsed = parse_body({"mimeType": "text/plain", "body": {"data": folded}})
	assert parsed.text == value and not parsed.missing
	assert decode_base64url(" +/8=\r\n") == decode_base64url("-_8=") == b"\xfb\xff"


@pytest.mark.parametrize("value", ["!!!", " \r\n\t", "a", "\u00a0SGVsbG8", "SGVsbG8?", "=SGVsbG8"])
def test_base64_normalization_still_rejects_malformed_transfer_data(value):
	with pytest.raises((ValueError, TypeError)):
		decode_base64url(value)
	parsed = parse_body({"mimeType": "text/plain", "body": {"data": value}})
	assert parsed.missing and not parsed.text


def test_browser_view_plain_stub_uses_substantive_html_without_following_links():
	text = "Your completed statement is available. " + "No reply is required. " * 8
	payload = {"mimeType": "multipart/alternative", "parts": [
		part("text/plain", "View this email in your browser: https://example.invalid/view"),
		part("text/html", f"<p>{text}</p>"),
	]}
	parsed = parse_body(payload)
	assert parsed.text == text.strip()
	assert not parsed.missing


def test_stub_does_not_replace_plain_with_an_even_less_informative_html_alternative():
	payload = {"mimeType": "multipart/alternative", "parts": [
		part("text/plain", "View in browser"), part("text/html", "<p>Hi</p>"),
	]}
	assert parse_body(payload).text == "View in browser"


@pytest.mark.parametrize("tag", ["head", "object", "noscript"])
def test_unclosed_non_executable_block_recovers_later_visible_body_with_review_flag(tag):
	parsed = parse_body(part("text/html", f"<{tag}>not visible<p>Actual message</p><p>Reply tomorrow</p>"))
	assert parsed.text == "Actual message\n\nReply tomorrow"
	assert parsed.truncated and not parsed.missing
	assert "not visible" not in parsed.text


def test_unclosed_head_keeps_script_and_style_inert_while_recovering_body():
	parsed = parse_body(part("text/html", "<head><style>p{color:red}</style><script>danger()</script><body><p>Visible</p>"))
	assert parsed.text == "Visible"
	assert parsed.truncated
	assert "danger" not in parsed.text and "color" not in parsed.text


def test_well_formed_block_fallback_is_not_recovered_or_misclassified_as_visible():
	parsed = parse_body(part("text/html", "<object><p>Hidden fallback</p></object><p>Visible</p>"))
	assert parsed.text == "Visible"
	assert not parsed.truncated
