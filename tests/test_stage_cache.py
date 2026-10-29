"""History-covered read snapshots retain live write preflight safety."""

import pytest

from gmail_clean.demo import DEMO_ACCOUNT, DemoMailbox
from gmail_clean.domain import Config, resolve_window
from gmail_clean.gmail import GmailError, HistoryExpired, PageTokenExpired
from gmail_clean.jobs import JobRunner
from gmail_clean.service import AppService
from gmail_clean.stage_cache import StageMetadataCache
from test_jobs import ANCHOR, PipelineService, RecordingAI, mail


def test_history_refresh_reuses_unchanged_evidence_and_invalidates_changed_thread():
	box = DemoMailbox(count=0, anchor=ANCHOR)
	message = mail("one", thread="shared")
	message.account_id = DEMO_ACCOUNT
	box.add_message(message)
	cache = StageMetadataCache(box)
	first = cache.metadata(["one"])
	cache.thread("shared")
	before = box.quota_limiter.consumed
	assert cache.metadata(["one"])["one"].labels == first["one"].labels
	cache.thread("shared")
	assert box.quota_limiter.consumed - before == 2
	box.modify(["one"], ["label_records"], [])
	assert "label_records" in cache.metadata(["one"])["one"].labels
	reply = mail("reply", age_days=1, thread="shared")
	reply.account_id = DEMO_ACCOUNT
	box.add_message(reply)
	cache.metadata(["one"])
	assert {message.id for message in cache.thread("shared")} == {"one", "reply"}


def test_cache_ttl_and_history_failures_never_authorize_stale_reuse():
	box = DemoMailbox(count=0, anchor=ANCHOR)
	message = mail("one")
	message.account_id = DEMO_ACCOUNT
	box.add_message(message)
	now = [0.0]
	cache = StageMetadataCache(box, 10, clock=lambda: now[0])
	cache.metadata(["one"])
	now[0] = 11
	before = box.quota_limiter.consumed
	cache.metadata(["one"])
	assert box.quota_limiter.consumed - before == 2
	box.inject_error("history", GmailError("network", "offline"))
	with pytest.raises(GmailError):
		cache.metadata(["one"])
	box.inject_error("history", HistoryExpired())
	before = box.quota_limiter.consumed
	cache.metadata(["one"])
	assert box.quota_limiter.consumed - before == 23


def test_stage_change_is_detected_before_classification(tmp_path):
	service = PipelineService(tmp_path / "state.sqlite", [mail("one", body="")])
	runner = JobRunner(service)
	try:
		job = runner.new()
		window = resolve_window(service.config, ANCHOR)
		runner.queue(job, "one")
		assert runner.process_stage(job, "metadata", window, service.config, None, None)
		service.mailbox.messages["one"].labels.remove("INBOX")
		service.mailbox.events.append({"id": "101", "labelsRemoved": [{"message": {"id": "one", "threadId": "one"}, "labelIds": ["INBOX"]}]})
		assert runner.process_stage(job, "body", window, service.config, None, None)
		assert not service.mailbox.body_fetches
		assert service.store.one("SELECT decision FROM messages WHERE id='one'")["decision"] == "skip"
	finally:
		service.store.close()


def test_cache_is_job_scoped_and_does_not_reuse_mutated_return_objects():
	box = DemoMailbox(count=0, anchor=ANCHOR)
	message = mail("one")
	message.account_id = DEMO_ACCOUNT
	box.add_message(message)
	cache = StageMetadataCache(box)
	message = cache.metadata(["one"])["one"]
	message.labels.clear()
	assert "INBOX" in cache.metadata(["one"])["one"].labels
	other_job = StageMetadataCache(box)
	before = box.quota_limiter.consumed
	other_job.metadata(["one"])
	assert box.quota_limiter.consumed - before == 21


def test_cache_setting_is_bounded_and_can_be_disabled():
	Config(metadata_cache_ttl_seconds=0).validate()
	with pytest.raises(ValueError):
		Config(metadata_cache_ttl_seconds=301).validate()


@pytest.mark.parametrize("error", [GmailError("network", "offline"), PageTokenExpired()])
def test_incomplete_history_page_never_advances_cursor_or_reuses_stale_data(monkeypatch, error):
	box = DemoMailbox(count=0, anchor=ANCHOR)
	message = mail("one")
	message.account_id = DEMO_ACCOUNT
	box.add_message(message)
	cache = StageMetadataCache(box)
	cache.metadata(["one"])
	cache.thread("one")
	start = cache.history_id
	calls = []
	def history(cursor, token=None):
		calls.append((cursor, token))
		if token:
			raise error
		return {"history": [{"messages": [{"id": "one", "threadId": "one"}]}], "nextPageToken": "second"}
	monkeypatch.setattr(box, "history", history)
	if isinstance(error, PageTokenExpired):
		assert cache.metadata(["one"])["one"].id == "one"
		assert not cache.threads
	else:
		with pytest.raises(GmailError):
			cache.metadata(["one"])
		assert cache.history_id == start
		assert not cache.messages and not cache.threads
	assert calls == [(start, None), (start, "second")]


def test_history_revalidation_retains_cache_across_long_stage_transitions():
	box = DemoMailbox(count=0, anchor=ANCHOR)
	message = mail("one")
	message.account_id = DEMO_ACCOUNT
	box.add_message(message)
	cache = StageMetadataCache(box, clock=box.quota_limiter.clock)
	cache.metadata(["one"])
	cache.thread("one")
	box.quota_limiter._advance(600)
	before = box.quota_limiter.consumed
	cache.metadata(["one"])
	cache.thread("one")
	assert box.quota_limiter.consumed - before == 2


def test_all_history_pages_are_consumed_before_reusing_evidence(monkeypatch):
	box = DemoMailbox(count=0, anchor=ANCHOR)
	message = mail("one")
	message.account_id = DEMO_ACCOUNT
	box.add_message(message)
	cache = StageMetadataCache(box)
	cache.metadata(["one"])
	cache.thread("one")
	start = cache.history_id
	calls = []
	def history(cursor, token=None):
		calls.append((cursor, token))
		if not token:
			return {"history": [{"messages": [{"id": f"other-{index}", "threadId": "other"}]} for index in range(500)], "nextPageToken": "second"}
		return {"history": [{"messages": [{"id": "one", "threadId": "one"}]}], "historyId": "999"}
	monkeypatch.setattr(box, "history", history)
	cache.metadata(["one"])
	assert calls == [(start, None), (start, "second")]
	assert cache.history_id == "999" and not cache.threads


def test_cached_batch_still_resolves_connection_generation_guard(tmp_path):
	class GuardedService(PipelineService):
		revoked = False
		@property
		def mailbox(self):
			if self.revoked:
				raise GmailError("auth", "Connection changed")
			return self._box
		@mailbox.setter
		def mailbox(self, value):
			self._box = value
	service = GuardedService(tmp_path / "state.sqlite", [mail("one")])
	try:
		runner = JobRunner(service)
		job = runner.new()
		runner._metadata(job, ["one"], service.config)
		service.revoked = True
		with pytest.raises(GmailError):
			runner._metadata(job, ["one"], service.config)
	finally:
		service.store.close()


def test_unchanged_pipeline_consumes_fewer_modeled_quota_units(tmp_path):
	measurements = {}
	for ttl in (0, 60):
		box = DemoMailbox(count=0, anchor=ANCHOR)
		for index in range(20):
			message = mail(str(index))
			message.account_id = DEMO_ACCOUNT
			box.add_message(message)
		ai = RecordingAI()
		config = Config(ai_module="gmail_clean.demo_ai", metadata_cache_ttl_seconds=ttl,
			learning_enabled=False, retrieval_batch_size=5)
		service = AppService(mailbox=box, demo=True, config=config, data_dir=tmp_path / str(ttl),
			clock=lambda: ANCHOR, ai_factory=lambda *args, _ai=ai, **kwargs: _ai)
		try:
			result = service.run_job(service.new_job())
			assert result["state"] == "completed" and result["failed"] == 0
			assert len(ai.calls) == 20
			measurements[ttl] = result["quota_usage"]
		finally:
			service.close()
	assert measurements[0] == {"discovery": 8, "metadata": 1200, "body": 2400, "classification": 1200, "total": 4808}
	assert measurements[60] == {"discovery": 8, "metadata": 1208, "body": 1208, "classification": 40, "total": 2464}
