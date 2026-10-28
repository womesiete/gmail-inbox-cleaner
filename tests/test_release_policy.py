"""Executable release-policy regressions for coverage and reproducible builds."""
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_coverage_gate_is_configured_for_the_application():
	config = tomllib.loads((ROOT / "pyproject.toml").read_text())
	coverage = config["tool"]["coverage"]
	assert coverage["run"]["source"] == ["gmail_clean"]
	assert coverage["report"]["fail_under"] >= 75
	assert "--cov=gmail_clean" in (ROOT / "scripts" / "ci_tests.py").read_text()
