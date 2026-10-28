"""Seeded, reproducible fuzz properties for the untrusted parser boundaries."""
import base64
import json
import random
import string

from gmail_clean.ai import parse_response
from gmail_clean.mime import decode_base64url, parse_body


def test_base64_roundtrip_and_mime_limits_across_seeded_binary_inputs():
	rng = random.Random(81024)
	for size in range(200):
		raw = rng.randbytes(size)
		encoded = base64.b64encode(raw).decode().rstrip("=")
		folded = "\r\n".join(encoded[index:index + 17] for index in range(0, len(encoded), 17))
		assert decode_base64url(folded) == raw
		payload = {"mimeType": "multipart/mixed", "parts": [
			{"mimeType": "text/plain", "body": {"data": folded}},
			{"mimeType": "application/octet-stream", "filename": "never-download.bin", "body": {"attachmentId": "private", "size": size}},
		]}
		parsed = parse_body(payload, lambda _: (_ for _ in ()).throw(AssertionError("attachment fetched")), max_chars=80)
		assert len(parsed.text) <= 80
		assert parsed.attachments[0]["name"] == "never-download.bin"


def test_seeded_ai_envelope_mutations_never_create_unrequested_assessments():
	rng = random.Random(249)
	values = [None, True, 0, 1.5, [], {}, "", "unexpected"]
	for _ in range(300):
		if rng.randrange(2):
			value = {"schema_version": rng.choice(values), "assessments": rng.choice(values)}
			text = json.dumps(value)
		else:
			text = "".join(rng.choices(string.printable, k=rng.randrange(150)))
		batch = parse_response(text, ["a", "b"], ["label_records"])
		assert set(batch.assessments) <= {"a", "b"}
		assert set(batch.assessments).isdisjoint(batch.errors)
		assert set(batch.assessments) | set(batch.errors) == {"a", "b"}


def test_mime_walker_depth_and_part_count_are_bounded():
	payload = {"mimeType": "text/plain", "body": {"data": "SGVsbG8="}}
	for _ in range(100):
		payload = {"mimeType": "multipart/mixed", "parts": [payload]}
	parsed = parse_body(payload)
	assert parsed.truncated and parsed.missing
	payload = {"mimeType": "multipart/mixed", "parts": [{"mimeType": "text/plain", "body": {"data": "SGVsbG8="}}] * 600}
	assert parse_body(payload).truncated
