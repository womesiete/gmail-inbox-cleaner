"""Optional provider classification remains explicit, bounded and privacy safe."""

import json

import pytest

from gmail_clean.ai import AIClient, AIError, _error_packet


@pytest.mark.parametrize("category,expected,kind", [
	("rate_limit", "RateLimitError", "rate_limit"),
	("network", "NetworkError", "network"),
	("transient", "TransientProviderError", "transient"),
])
def test_provider_hook_classifies_nonstandard_errors_and_preserves_retry_after(tmp_path, category, expected, kind):
	module = tmp_path / "hook_provider.py"
	module.write_text(
		"class ProviderBusy(Exception):\n\tpass\n"
		"def call_api(prompt):\n\traise ProviderBusy('PRIVATE MESSAGE AND API KEY')\n"
		f"def classify_exception(exc):\n\treturn {{'category': {category!r}, 'retry_after': 125.5}}\n"
	)
	with AIClient(str(module)) as client, pytest.raises(AIError) as captured:
		client.call("private prompt")
	error = captured.value
	assert error.category == expected
	assert error.kind == kind and error.transient
	assert error.retry_after == 125.5
	assert error.to_dict()["retry_after"] == 125.5
	assert error.exception_class == "ProviderBusy"
	assert "PRIVATE MESSAGE" not in json.dumps(error.to_dict())


def test_plain_runtime_error_without_hook_is_not_automatically_retryable(tmp_path):
	module = tmp_path / "unknown_provider.py"
	module.write_text("def call_api(prompt):\n\traise RuntimeError('PRIVATE CONTENT')\n")
	with AIClient(str(module)) as client, pytest.raises(AIError) as captured:
		client.call("private prompt")
	assert captured.value.category == "UnknownProviderError"
	assert not captured.value.transient
	assert captured.value.retry_after is None


@pytest.mark.parametrize("delay", [-1, float("inf"), float("nan"), True, "125", None])
def test_invalid_retry_after_is_discarded(delay):
	packet = _error_packet(RuntimeError("private"), classifier=lambda exc: {"category": "network", "retry_after": delay})
	assert packet["category"] == "NetworkError"
	assert packet["retry_after"] is None
	assert packet["retry_after_invalid"] is (delay is not None)


def test_classifier_failures_and_unknown_categories_remain_unknown():
	def broken(exc):
		raise ValueError("PRIVATE CONTENT")
	for classifier in (broken, lambda exc: {"category": "invented"}, lambda exc: None):
		packet = _error_packet(RuntimeError("private"), classifier=classifier)
		assert packet["category"] == "UnknownProviderError"
		assert "PRIVATE CONTENT" not in json.dumps(packet)


def test_legacy_exported_rate_limit_preserves_numeric_retry_after():
	class RateLimitError(Exception):
		retry_after = 95
	packet = _error_packet(RateLimitError("private"), {"RateLimitError": RateLimitError})
	assert packet["category"] == "RateLimitError"
	assert packet["retry_after"] == 95


@pytest.mark.parametrize("value,invalid", [("float('inf')", True), ("1e308", False)])
def test_extreme_retry_after_crosses_ipc_without_losing_invalid_delay_signal(tmp_path, value, invalid):
	module = tmp_path / "delay_provider.py"
	module.write_text(
		"def call_api(prompt):\n\traise RuntimeError('PRIVATE CONTENT')\n"
		f"def classify_exception(exc):\n\treturn {{'category': 'network', 'retry_after': {value}}}\n"
	)
	with AIClient(str(module)) as client, pytest.raises(AIError) as captured:
		client.call("private prompt")
	error = captured.value
	assert error.retry_after_invalid is invalid
	assert error.to_dict()["retry_after_invalid"] is invalid
	assert error.retry_after == (None if invalid else 1e308)
