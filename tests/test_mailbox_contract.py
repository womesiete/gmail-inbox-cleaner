"""One mailbox protocol contract against demo and recorded Google HTTP fixtures."""
import base64
from datetime import UTC, datetime
import json

from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence
import pytest

from gmail_clean.demo import DEMO_ACCOUNT, DemoMailbox
from gmail_clean.domain import Message
from gmail_clean.gmail import GmailError, GmailMailbox


@pytest.fixture(params=["demo", "recorded-gmail"])
def mailbox(request):
	item = Message(id="contract1", thread_id="thread1", internal_ms=1_700_000_000_000,
		labels={"INBOX", "UNREAD"}, headers={"From": "sender@example.invalid", "Subject": "Recorded fixture"},
		body="Synthetic fixture text only.", account_id=DEMO_ACCOUNT)
	if request.param == "demo":
		box = DemoMailbox(count=0, anchor=datetime(2026, 10, 28, tzinfo=UTC))
		box.add_message(item)
		return box
	message = {"id": item.id, "threadId": item.thread_id, "internalDate": str(item.internal_ms),
		"labelIds": sorted(item.labels), "payload": {"mimeType": "text/plain",
			"headers": [{"name": key, "value": value} for key, value in item.headers.items()],
			"body": {"data": base64.urlsafe_b64encode(item.body.encode()).decode()}}}
	after = {**message, "labelIds": ["UNREAD", "label_records"]}
	# HttpMockSequence is the actual Google client's fixture transport. Every
	# response is synthetic; discovery comes from the package's static document.
	responses = [
		{"emailAddress": DEMO_ACCOUNT, "historyId": "2"},
		{"labels": [{"id": "label_records", "name": "Reference/Records", "type": "user"}]},
		{"messages": [{"id": item.id, "threadId": item.thread_id}]}, message, message,
		{"messages": [message]}, {"history": [], "historyId": "2"}, {}, after,
	]
	http = HttpMockSequence([({"status": "200", "content-type": "application/json"}, json.dumps(value)) for value in responses])
	service = build("gmail", "v1", http=http, static_discovery=True, cache_discovery=False)
	return GmailMailbox(service, account_id=DEMO_ACCOUNT)


def test_mailbox_protocol_identity_metadata_text_thread_history_and_label_delta(mailbox):
	assert mailbox.identity() == DEMO_ACCOUNT
	assert any(item["id"] == "label_records" for item in mailbox.labels())
	assert mailbox.list_messages("label:INBOX")["messages"][0]["id"] == "contract1"
	metadata = mailbox.get_message("contract1", body=False)
	assert metadata.body is None and metadata.labels == {"INBOX", "UNREAD"}
	full = mailbox.get_message("contract1", body=True)
	assert full.body == "Synthetic fixture text only." and not full.missing_content
	assert mailbox.thread_metadata("thread1")[0].body is None
	assert "historyId" in mailbox.history("2")
	mailbox.modify(["contract1"], ["label_records"], ["INBOX"])
	after = mailbox.get_message("contract1", body=True)
	assert after.labels == {"UNREAD", "label_records"}
	assert after.body == full.body


def test_both_adapters_forbid_system_label_and_destructive_deltas(mailbox):
	# This local guard must run before either transport can send a write.
	mailbox.labels()
	with pytest.raises(GmailError, match="Only INBOX"):
		mailbox.modify(["contract1"], [], ["UNREAD"])
