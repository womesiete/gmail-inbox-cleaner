from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import json

import pytest

from gmail_clean.auth import TokenStore
from gmail_clean.demo import DEMO_ACCOUNT, DemoMailbox, generate_messages
from gmail_clean.domain import Message, epoch_ms
from gmail_clean.gmail import GmailError, GmailMailbox, HistoryExpired, PageTokenExpired, translate_error


class FakeRequest:
	def __init__(self, value):
		self.value = value

	def execute(self, **kwargs):
		if isinstance(self.value, Exception):
			raise self.value
		return self.value


class HttpFailure(Exception):
	def __init__(self, status, reason=None, retry_after=None):
		self.resp = Response(status, retry_after)
		self.content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()


class Response(dict):
	def __init__(self, status, retry_after):
		super().__init__({"retry-after": retry_after})
		self.status = status


class FakeGoogle:
	def __init__(self):
		self.calls = []
		self.payloads = {}
		self.batches = []
		self.results = {}

	def users(self):
		return self

	def messages(self):
		return self

	def threads(self):
		return self

	def labels(self):
		return self

	def history(self):
		return self

	def getProfile(self, **kwargs):
		self.calls.append(("profile", kwargs))
		return FakeRequest({"emailAddress": "person@example.invalid", "historyId": "100"})

	def get(self, **kwargs):
		self.calls.append(("get", kwargs))
		return FakeRequest(self.payloads.get(kwargs["id"], {}))

	def list(self, **kwargs):
		self.calls.append(("list", kwargs))
		return FakeRequest(self.results.get("list", {"labels": [{"id": "user1", "name": "Records", "type": "user"}]}))

	def batchModify(self, **kwargs):
		self.calls.append(("batchModify", kwargs))
		return FakeRequest({})

	def new_batch_http_request(self, callback):
		parent = self
		class Batch:
			def __init__(self):
				self.entries = []
			def add(self, request, request_id):
				self.entries.append((request_id, request))
			def execute(self):
				parent.batches.append(len(self.entries))
				for message_id, request in self.entries:
					try:
						callback(message_id, request.execute(), None)
					except Exception as exc:
						callback(message_id, None, exc)
		return Batch()


def gmail_message(message_id):
	return {"id": message_id, "threadId": "thread1", "internalDate": "10000",
		"labelIds": ["INBOX", "UNREAD", "STARRED", "IMPORTANT", "other"],
		"snippet": "hi", "payload": {"mimeType": "text/plain", "headers": [{"name": "From", "value": "A <a@example.invalid>"}],
			"body": {"data": "SGVsbG8"}}}


def test_gmail_metadata_does_not_fetch_body_or_mark_read_and_batches_handle_parts():
	service = FakeGoogle()
	service.payloads = {str(index): gmail_message(str(index)) for index in range(5)}
	service.payloads["2"] = HttpFailure(404)
	box = GmailMailbox(service, "person@example.invalid", retrieval_batch_size=2)
	results = box.get_messages(["0", "1", "2", "3", "4"], False)
	assert service.batches == [2, 2, 1]
	assert isinstance(results["2"], GmailError) and results["2"].kind == "missing"
	assert results["0"].body is None and results["0"].missing_content
	assert results["0"].labels == {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "other"}
	assert all(call[1]["format"] == "metadata" for call in service.calls if call[0] == "get")
	assert not any(call[0] == "batchModify" for call in service.calls)
	assert box.get_message("0", True).body == "Hello"


def test_gmail_write_guard_batches_minimal_deltas_and_history_has_no_inbox_filter():
	service = FakeGoogle()
	box = GmailMailbox(service, execution_batch_size=2)
	box.modify(["a", "b", "c"], ["user1"], ["INBOX"])
	writes = [params["body"] for name, params in service.calls if name == "batchModify"]
	assert writes == [{"ids": ["a", "b"], "addLabelIds": ["user1"], "removeLabelIds": ["INBOX"]},
		{"ids": ["c"], "addLabelIds": ["user1"], "removeLabelIds": ["INBOX"]}]
	for forbidden in ("TRASH", "UNREAD", "STARRED", "IMPORTANT", "missing-user-label"):
		with pytest.raises(GmailError):
			box.modify(["a"], [], [forbidden])
	box.history("100")
	params = service.calls[-1][1]
	assert params["startHistoryId"] == "100" and "labelId" not in params and "historyTypes" not in params


def test_errors_are_structured_and_never_include_provider_sensitive_payloads():
	assert isinstance(translate_error(HttpFailure(404), "history"), HistoryExpired)
	assert isinstance(translate_error(HttpFailure(400), "list", True), PageTokenExpired)
	assert isinstance(translate_error(HttpFailure(400), "history", True), PageTokenExpired)
	assert translate_error(HttpFailure(403, "userRateLimitExceeded", "4")).retry_after == 4
	assert translate_error(HttpFailure(403, "userRateLimitExceeded")).transient
	assert not translate_error(HttpFailure(403, "insufficientPermissions")).transient
	assert translate_error(HttpFailure(401)).kind == "auth"
	assert translate_error(HttpFailure(503)).transient
	assert "password" not in str(translate_error(RuntimeError("password=secret token=secret body=private")))


def test_demo_persistence_exact_ids_and_unknown_write_outcome(tmp_path):
	path = tmp_path / "demo.json"
	anchor = datetime(2026, 10, 28, 12, tzinfo=UTC)
	box = DemoMailbox(path, count=150, anchor=anchor)
	item = next(message for message in box.messages.values() if "INBOX" in message.labels)
	before = set(item.labels)
	start = box.profile()["historyId"]
	box.lose_next_modify_response = True
	with pytest.raises(GmailError):
		box.modify([item.id], ["label_records"], ["INBOX"])
	assert box.get_message(item.id).labels == (before | {"label_records"}) - {"INBOX"}
	restored = DemoMailbox(path, count=1)
	assert len(restored.messages) == 150
	assert restored.identity() == DEMO_ACCOUNT
	assert restored.get_message(item.id).labels == box.get_message(item.id).labels
	changes = restored.history(start)["history"]
	assert any(event.get("labelsRemoved") for event in changes)
	assert box.body_reads == []


def test_demo_examination_boundary_and_thread_metadata_leak_no_bodies():
	anchor = datetime(2026, 10, 28, 12, tzinfo=UTC)
	box = DemoMailbox(count=70, anchor=anchor)
	quiet = box.messages[f"d{32:015x}"]
	previous = int((anchor - timedelta(days=14)).timestamp())
	current = int((anchor + timedelta(days=1) - timedelta(days=14)).timestamp())
	result = box.list_messages(f"label:INBOX after:{previous - 86400} before:{current + 86400}")
	assert quiet.id in {item["id"] for item in result["messages"]}
	assert previous * 1000 <= quiet.internal_ms < current * 1000
	old = box.messages[f"d{30:015x}"]
	context = box.thread_metadata(old.thread_id)
	assert len(context) == 2 and all(not item.body for item in context)
	assert box.body_reads == []


def test_demo_page_invalidation_missing_and_account_guards():
	box = DemoMailbox(count=40)
	page = box.list_messages("label:INBOX", page_size=3)
	message_id = page["messages"][0]["id"]
	box.modify([message_id], [], ["INBOX"])
	with pytest.raises(PageTokenExpired):
		box.list_messages("label:INBOX", page["nextPageToken"])
	with pytest.raises(HistoryExpired):
		box.history("0")
	with pytest.raises(GmailError):
		box.add_message(Message("x", "x", epoch_ms(datetime.now(UTC)), account_id="live@example.invalid"))
	assert isinstance(box.get_messages(["absent"])["absent"], GmailError)


def test_token_store_never_falls_back_to_plaintext(tmp_path, monkeypatch):
	store = TokenStore(tmp_path)
	credentials = SimpleNamespace(refresh_token="private", token_uri="uri", client_id="id", client_secret="secret", scopes=["modify"])
	monkeypatch.setattr(TokenStore, "_keyring", staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))))
	monkeypatch.setattr(TokenStore, "_dpapi", staticmethod(lambda data, encrypt: b"encrypted" if encrypt else b'{}'))
	store.save(credentials)
	assert store.path.read_bytes() == b"encrypted"
	assert store.load() == {}
	assert len(list(tmp_path.iterdir())) == 1


def test_synthetic_generator_has_pre_2000_messages_and_mixed_sender_intent():
	messages = list(generate_messages(100, datetime(2026, 10, 28, tzinfo=UTC)))
	assert len(messages) == 100
	assert min(item.internal_date.year for item in messages) < 2000
	case = [item for item in messages if item.sender == "case@services.example.invalid"]
	assert any("completed" in item.subject for item in case)
	assert any("Please reply" in item.subject for item in case)
	assert any("unclear" in item.subject for item in case)


def test_real_adapter_respects_configured_body_character_limit_above_default():
	import base64
	service = FakeGoogle()
	payload = gmail_message("large")
	payload["payload"]["body"]["data"] = base64.urlsafe_b64encode(("x" * 80_000).encode()).decode()
	service.payloads["large"] = payload
	box = GmailMailbox(service, body_limit=70_000)
	message = box.get_message("large", body=True)
	assert len(message.body) == 70_000 and message.truncated
