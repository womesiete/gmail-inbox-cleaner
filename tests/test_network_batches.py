"""Exercise real application batch caches through the durable job runner."""
from gmail_clean.domain import Config, context_hash, content_hash, resolve_window
from gmail_clean.service import AppService
from test_jobs import ANCHOR, RecordingAI, RecordingMailbox, mail


def test_real_classification_batch_reuses_policy_thread_and_context_reads(tmp_path):
	targets = [mail(f"target-{index}", thread="shared") for index in range(3)]
	context = [mail(f"sent-{index}", thread="shared", labels={"SENT"}, age_days=60) for index in range(2)]
	mailbox = RecordingMailbox([*targets, *context])
	ai = RecordingAI()
	config = Config(ai_module="gmail_clean.demo_ai", ai_batch_size=3, sent_context_enabled=True, learning_enabled=False)
	service = AppService(mailbox=mailbox, config=config, data_dir=tmp_path, clock=lambda: ANCHOR,
		ai_factory=lambda *args, **kwargs: ai)
	try:
		members = mailbox.thread_metadata("shared")
		for message in targets:
			message.context_version = context_hash(members, True)
			message.content_hash = content_hash(message)
			service.store.upsert_message(message)
		job = service.new_job()
		for message in targets:
			service.jobs.queue(job, message.id, "classification")
		queries = []
		service.store.conn.set_trace_callback(queries.append)
		mailbox.thread_fetches.clear()
		mailbox.body_fetches.clear()
		assert service.jobs.process_stage(job, "classification", resolve_window(config, ANCHOR), config, None, None)
		assert len(ai.calls) == 1 and len(ai.calls[0]["context_messages"]) == 2
		assert len(mailbox.thread_fetches) == 1
		assert mailbox.body_fetches == ["sent-0", "sent-1"]
		assert sum("SELECT * FROM manual_overrides" in sql for sql in queries) == 1
		assert sum("WHERE key='rules_revision'" in sql for sql in queries) == 1
	finally:
		service.close()
