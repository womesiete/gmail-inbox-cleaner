"""Executable release-policy regressions for coverage and reproducible builds."""
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_coverage_gate_is_configured_for_the_application():
	config = tomllib.loads((ROOT / "pyproject.toml").read_text())
	coverage = config["tool"]["coverage"]
	assert coverage["run"]["source"] == ["gmail_clean"]
	assert coverage["report"]["fail_under"] >= 75
	assert "--cov=gmail_clean" in (ROOT / "scripts" / "ci_tests.py").read_text()


def test_ci_pins_actions_audits_dependencies_checks_types_and_builds_windows():
	workflow = (ROOT / ".github/workflows/checks.yml").read_text()
	refs = re.findall(r"uses:\s+\S+@([^\s]+)", workflow)
	assert refs and all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs)
	assert "pip_audit" in workflow and "mypy" in workflow
	assert "scripts/package_windows.ps1" in workflow
	assert (ROOT / "constraints.txt").is_file()
