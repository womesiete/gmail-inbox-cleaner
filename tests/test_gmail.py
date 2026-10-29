from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

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
					except Exception as exc:  # noqa: BLE001 - emulate Google's per-part failure callback
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
	assert isinstance(translate_error(HttpFailure(400, "invalidPageToken"), "list", True), PageTokenExpired)
	assert isinstance(translate_error(HttpFailure(400, "invalidPageToken"), "history", True), PageTokenExpired)
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
	assert {path.name for path in tmp_path.iterdir()} == {"oauth.dpapi", "oauth.backend"}
	assert store.backend_path.read_text() == "dpapi"


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


def test_demo_instances_observe_commits_without_reparsing_unchanged_state(tmp_path, monkeypatch):
	path = tmp_path / "demo.json"
	parent = DemoMailbox(path, count=40)
	worker = DemoMailbox(path)
	mid = next(message.id for message in parent.messages.values() if "INBOX" in message.labels)
	loads = []
	original_load = parent._load_from_disk
	def tracked_load():
		loads.append(True)
		return original_load()
	monkeypatch.setattr(parent, "_load_from_disk", tracked_load)
	start = parent.profile()["historyId"]
	for _ in range(3):
		parent.get_message(mid)
		parent.get_messages([mid])
		parent.labels()
		parent.thread_metadata(parent.messages[mid].thread_id)
		parent.history(start)
	assert not loads
	worker.modify([mid], ["label_records"], ["INBOX"])
	assert parent.profile()["historyId"] != start
	assert "label_records" in parent.get_messages([mid])[mid].labels
	assert "INBOX" not in parent.get_message(mid).labels
	assert mid not in {item["id"] for item in parent.list_messages("label:INBOX")["messages"]}
	assert parent.history(start)["history"]
	new = Message("added", "new-thread", epoch_ms(datetime.now(UTC)), {"INBOX"}, body="synthetic", account_id=DEMO_ACCOUNT)
	worker.add_message(new)
	assert [message.id for message in parent.thread_metadata("new-thread")] == ["added"]
	worker._labels[0]["name"] = "Renamed synthetic label"
	worker._save()
	assert parent.labels()[0]["name"] == "Renamed synthetic label"
	assert len(loads) == 3  # One reload per committed disk generation, never per read.


def test_concurrent_demo_adapters_preserve_both_label_changes_and_unique_history(tmp_path):
	path = tmp_path / "demo.json"
	first = DemoMailbox(path, count=40)
	second = DemoMailbox(path)
	mid = next(iter(first.messages))
	barrier = threading.Barrier(2)
	def write(item):
		box, label = item
		barrier.wait(timeout=10)
		box.modify([mid], [label], [])
	with ThreadPoolExecutor(max_workers=2) as pool:
		list(pool.map(write, [(first, "label_records"), (second, "label_personal")]))
	assert {"label_records", "label_personal"} <= first.get_message(mid).labels
	assert first.get_message(mid).labels == second.get_message(mid).labels
	assert len({event["id"] for event in first.history("1")["history"]}) == 2
	assert not list(tmp_path.glob("*.tmp"))


def test_changed_demo_read_holds_the_cross_process_replace_lock(tmp_path, monkeypatch):
	path = tmp_path / "demo.json"
	reader = DemoMailbox(path, count=40)
	writer = DemoMailbox(path)
	mid = next(iter(reader.messages))
	writer.modify([mid], ["label_personal"], [])
	inside = []
	original_lock = reader._disk_write_lock
	original_load = reader._load_from_disk
	@contextmanager
	def tracked_lock():
		with original_lock():
			inside.append(True)
			try:
				yield
			finally:
				inside.pop()
	def guarded_load():
		# A reader's open file handle must participate in the same OS lock used
		# by replacement writers; this catches the Windows share-delete race
		# deterministically rather than relying on a narrow scheduling window.
		assert inside
		return original_load()
	monkeypatch.setattr(reader, "_disk_write_lock", tracked_lock)
	monkeypatch.setattr(reader, "_load_from_disk", guarded_load)
	assert "label_personal" in reader.get_message(mid).labels
	assert not inside


def test_demo_processes_serialize_read_modify_write(tmp_path):
	path = tmp_path / "demo.json"
	box = DemoMailbox(path, count=40)
	mid = next(iter(box.messages))
	go = tmp_path / "go"
	code = """import sys,time
from pathlib import Path
from gmail_clean.demo import DemoMailbox
box=DemoMailbox(sys.argv[1])
original=box._save
def slow_save():
\ttime.sleep(0.1)
\toriginal()
box._save=slow_save
Path(sys.argv[4]).write_text('ready')
deadline=time.monotonic()+30
while not Path(sys.argv[5]).exists():
\tif time.monotonic()>deadline: raise RuntimeError('parent did not release barrier')
\ttime.sleep(0.01)
box.modify([sys.argv[2]],[sys.argv[3]],[])
"""
	processes = []
	try:
		for index, label in enumerate(("label_records", "label_personal")):
			processes.append(subprocess.Popen([sys.executable, "-c", code, str(path), mid, label, str(tmp_path / f"ready-{index}"), str(go)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
		deadline = time.monotonic() + 30
		while not all((tmp_path / f"ready-{index}").exists() for index in range(2)):
			assert time.monotonic() < deadline, "Demo processes did not reach the shared write barrier"
			time.sleep(0.01)
		go.write_text("go")
		for process in processes:
			stdout, stderr = process.communicate(timeout=30)
			assert process.returncode == 0, stdout + stderr
	finally:
		for process in processes:
			if process.poll() is None:
				process.terminate()
				process.wait(timeout=10)
	assert {"label_records", "label_personal"} <= box.get_message(mid).labels
	assert len(box.history("1")["history"]) == 2
	assert not list(tmp_path.glob("*.tmp"))


def test_failed_demo_atomic_replace_keeps_previous_state_and_cleans_unique_temporary(tmp_path, monkeypatch):
	from gmail_clean import demo
	path = tmp_path / "demo.json"
	box = DemoMailbox(path, count=40)
	mid = next(iter(box.messages))
	before = path.read_bytes()
	seen = []
	def failed_replace(source, target):
		seen.append(Path(source).name)
		raise PermissionError("synthetic atomic replace failure")
	monkeypatch.setattr(demo.os, "replace", failed_replace)
	with pytest.raises(PermissionError):
		box.modify([mid], ["label_personal"], [])
	assert path.read_bytes() == before
	assert "label_personal" not in box.get_message(mid).labels
	assert seen[0].startswith("demo.json.") and seen[0].endswith(".tmp")
	assert not list(tmp_path.glob("*.tmp"))


def test_protected_token_saves_use_distinct_temporary_files_and_cleanup_on_failure(tmp_path, monkeypatch):
	from gmail_clean import auth
	store = TokenStore(tmp_path)
	monkeypatch.setattr(TokenStore, "_keyring", staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))))
	monkeypatch.setattr(TokenStore, "_dpapi", staticmethod(lambda data, encrypt: b"encrypted:" + data if encrypt else data.removeprefix(b"encrypted:")))
	original_replace = auth.os.replace
	barrier = threading.Barrier(2)
	paths = []
	def concurrent_replace(source, target):
		if Path(target) == store.path:
			paths.append(Path(source))
		original_replace(source, target)
	monkeypatch.setattr(auth.os, "replace", concurrent_replace)
	def save_token(token):
		barrier.wait(timeout=10)
		store.save(SimpleNamespace(refresh_token=token, token_uri="uri", client_id="id", client_secret="secret", scopes=["modify"]))
	with ThreadPoolExecutor(max_workers=2) as pool:
		list(pool.map(save_token, ["synthetic-first", "synthetic-second"]))
	assert len(set(paths)) == 2
	assert store.load()["refresh_token"] in {"synthetic-first", "synthetic-second"}
	assert not list(tmp_path.glob("*.tmp"))
	def failed_replace(source, target):
		raise PermissionError("synthetic replace failure")
	monkeypatch.setattr(auth.os, "replace", failed_replace)
	with pytest.raises(PermissionError):
		store.save(SimpleNamespace(refresh_token="synthetic-third", token_uri="uri", client_id="id", client_secret="secret", scopes=["modify"]))
	assert not list(tmp_path.glob("*.tmp"))


def test_token_readers_replacements_and_delete_use_one_shared_path_lock(tmp_path, monkeypatch):
	from gmail_clean import auth
	stores = [TokenStore(tmp_path), TokenStore(tmp_path / "alias" / "..")]
	target = stores[0].path.resolve()
	monkeypatch.setattr(TokenStore, "_keyring", staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))))
	monkeypatch.setattr(TokenStore, "_dpapi", staticmethod(lambda data, encrypt: b"encrypted:" + data if encrypt else data.removeprefix(b"encrypted:")))
	monitor = threading.Lock()
	busy = {"readers": 0, "writers": 0}
	original_read = Path.read_bytes
	original_replace = auth.os.replace
	original_unlink = Path.unlink
	original_lock = auth.path_lock
	owners = set()
	observed = set()
	@contextmanager
	def tracked_lock(path):
		with original_lock(path):
			owner = threading.get_ident()
			with monitor:
				assert not owners, "Distinct stores must serialize the same protected file"
				owners.add(owner)
			try:
				yield
			finally:
				with monitor:
					owners.remove(owner)
	def read_bytes(path):
		if path.resolve() != target:
			return original_read(path)
		with monitor:
			assert threading.get_ident() in owners
			assert not busy["writers"]
			busy["readers"] += 1
			observed.add("read")
		try:
			# Simulate a Windows reader that denies FILE_SHARE_DELETE while open.
			time.sleep(0.002)
			return original_read(path)
		finally:
			with monitor:
				busy["readers"] -= 1
	def replace(source, destination):
		if Path(destination).resolve() != target:
			return original_replace(source, destination)
		with monitor:
			assert threading.get_ident() in owners
			if busy["readers"] or busy["writers"]:
				raise PermissionError("simulated Windows replacement sharing violation")
			busy["writers"] += 1
			observed.add("replace")
		try:
			time.sleep(0.002)
			return original_replace(source, destination)
		finally:
			with monitor:
				busy["writers"] -= 1
	def unlink(path, *args, **kwargs):
		if path.resolve() == target:
			assert threading.get_ident() in owners
			observed.add("delete")
		return original_unlink(path, *args, **kwargs)
	monkeypatch.setattr(auth, "path_lock", tracked_lock)
	monkeypatch.setattr(Path, "read_bytes", read_bytes)
	monkeypatch.setattr(auth.os, "replace", replace)
	monkeypatch.setattr(Path, "unlink", unlink)
	barrier = threading.Barrier(8)
	def work(index):
		store = stores[index % len(stores)]
		barrier.wait(timeout=10)
		for version in range(4):
			store.save(SimpleNamespace(refresh_token=f"synthetic-{index}-{version}", token_uri="uri", client_id="id", client_secret="secret", scopes=["modify"]))
			value = store.load()
			assert value is None or value["refresh_token"].startswith("synthetic-")
			if version == 3:
				store.delete()
	with ThreadPoolExecutor(max_workers=8) as pool:
		list(pool.map(work, range(8)))
	assert observed == {"read", "replace", "delete"}
	assert not owners and busy == {"readers": 0, "writers": 0}
	assert not list(tmp_path.glob("*.tmp"))


def test_page_token_error_translation_does_not_mask_other_bad_requests():
	assert translate_error(HttpFailure(400, "invalidArgument"), "list", True).kind == "data"
	assert isinstance(translate_error(HttpFailure(400, "invalidPageToken"), "list", True), PageTokenExpired)


def test_quota_limiter_accounts_current_metadata_body_and_batch_units():
	from gmail_clean.gmail import QuotaLimiter
	now = [0.0]
	waits = []
	def sleep(delay):
		waits.append(delay)
		now[0] += delay
	limiter = QuotaLimiter(units_per_minute=6000, clock=lambda: now[0], sleep=sleep)
	service = FakeGoogle()
	service.payloads = {"a": gmail_message("a"), "b": gmail_message("b")}
	box = GmailMailbox(service, quota_limiter=limiter)
	box.get_messages(["a", "b"], body=False)  # 2 * 20, not one multipart unit.
	box.get_messages(["a", "b"], body=True)  # 2 * 60.
	assert limiter.consumed == 160
	limiter.acquire(5840)
	limiter.acquire(20)
	assert waits and waits[-1] == pytest.approx(60)
	assert limiter.consumed == 6020


def test_idempotent_read_retries_with_jitter_but_modify_is_not_retried():
	service = FakeGoogle()
	box = GmailMailbox(service)
	waits = []
	box._sleep = waits.append
	box._random = lambda: 0.25
	class Flaky:
		def __init__(self):
			self.calls = 0
		def execute(self, **kwargs):
			self.calls += 1
			if self.calls < 3:
				raise HttpFailure(503)
			return {"ok": True}
	request = Flaky()
	assert box._execute(request) == {"ok": True}
	assert waits == [1.25, 2.25]
	request = Flaky()
	with pytest.raises(GmailError):
		box._execute(request, "modify", idempotent=False, quota_units=50)
	assert request.calls == 1


def _refresh_transport_errors():
	import socket
	import ssl
	from google.auth.exceptions import RefreshError, TransportError
	from httplib2 import ServerNotFoundError
	from requests.exceptions import ConnectionError, Timeout
	return [TransportError('private'), ConnectionError('private'), Timeout('private'),
		ServerNotFoundError('private'), ssl.SSLError('private'), socket.timeout('private'),
		OSError('private'), RefreshError('temporary provider failure')]


@pytest.mark.parametrize('failure', _refresh_transport_errors())
def test_refresh_transport_errors_remain_retryable_through_execute_and_batch(failure):
	service = FakeGoogle()
	box = GmailMailbox(service)
	delays = []
	box._sleep = delays.append
	with pytest.raises(GmailError) as caught:
		box._execute(FakeRequest(failure))
	assert caught.value.kind == ('transient' if type(failure).__name__ == 'RefreshError' else 'network')
	assert caught.value.transient and len(delays) == 2
	assert 'private' not in caught.value.safe_message
	service.payloads['failed'] = failure
	batch_error = box.get_messages(['failed'])['failed']
	assert isinstance(batch_error, GmailError)
	assert batch_error.kind == caught.value.kind and batch_error.transient


@pytest.mark.parametrize('detail', ['invalid_grant: revoked', {'error': 'invalid_grant'}])
def test_invalid_grant_remains_auth_failure_through_execute_and_batch(detail):
	from google.auth.exceptions import RefreshError
	failure = RefreshError(detail)
	service = FakeGoogle()
	box = GmailMailbox(service)
	delays = []
	box._sleep = delays.append
	with pytest.raises(GmailError) as caught:
		box._execute(FakeRequest(failure))
	assert caught.value.kind == 'auth' and not caught.value.transient and not delays
	service.payloads['failed'] = failure
	assert box.get_messages(['failed'])['failed'].kind == 'auth'


@pytest.mark.parametrize('catalog_failure', [False, True])
def test_write_catalog_failures_are_definitely_unsent(catalog_failure):
	from gmail_clean.gmail import PreSendError
	service = FakeGoogle()
	box = GmailMailbox(service)
	box._sleep = lambda _: None
	service.results['list'] = HttpFailure(503) if catalog_failure else {'labels': []}
	with pytest.raises(PreSendError) as caught:
		box.modify(['a'], ['user1'], [])
	assert caught.value.sent is False
	assert caught.value.kind == ('transient' if catalog_failure else 'permission')
	assert not any(name == 'batchModify' for name, _ in service.calls)


def test_quota_failure_before_write_is_definitely_unsent():
	from gmail_clean.gmail import PreSendError
	service = FakeGoogle()
	box = GmailMailbox(service)
	box._catalog = [{'id': 'user1', 'type': 'user'}]
	class FailedLimiter:
		def acquire(self, units):
			raise GmailError('network', 'Synthetic limiter unavailable')
	box.quota_limiter = FailedLimiter()
	with pytest.raises(PreSendError) as caught:
		box.modify(['a'], ['user1'], [])
	assert caught.value.sent is False and caught.value.kind == 'network'
