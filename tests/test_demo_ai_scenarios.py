"""The offline provider covers the documented synthetic review scenarios."""
import json

import pytest

from gmail_clean import demo_ai


@pytest.mark.parametrize(("body", "category"), [
	("Promotion: sale ended", "promotion"),
	("Receipt: payment received", "receipt"),
	("Financial bank statement for records", "financial"),
	("Medical patient record for reference", "medical"),
	("Legal signed agreement", "legal"),
	("Event: conference concluded", "event"),
	("Notification: delivery complete", "notification"),
	("Project completed and filed", "work_request"),
])
def test_public_demo_contract_exercises_completed_scenarios(body, category):
	message = {"message_id": "synthetic", "headers": {}, "body": body}
	result = demo_ai.call_api_result(json.dumps({"messages": [message]}))
	assessment = json.loads(result.text)["assessments"][0]
	assert assessment["category"] == category
	assert assessment["outstanding_action"] == "none"
	assert assessment["keep_in_inbox_score"] < 50
	assert result.provider == "offline-synthetic-demo"


def test_public_demo_contract_treats_incomplete_status_as_uncertain():
	message = {"message_id": "synthetic", "headers": {}, "body": "Completion uncertain; status unclear"}
	assessment = json.loads(demo_ai.call_api(json.dumps({"messages": [message]})))["assessments"][0]
	assert assessment["outstanding_action"] == "uncertain"
	assert assessment["uncertainty"] == "high"
