"""Executable release-policy regressions for coverage and reproducible builds."""
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_coverage_gate_is_configured_for_the_application():
	config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
	coverage = config["tool"]["coverage"]
	assert coverage["run"]["source"] == ["gmail_clean"]
	assert coverage["report"]["fail_under"] >= 85
	assert "--cov=gmail_clean" in (ROOT / "scripts" / "ci_tests.py").read_text(encoding="utf-8")


def test_ci_pins_actions_audits_dependencies_checks_types_and_builds_windows():
	workflow = (ROOT / ".github/workflows/checks.yml").read_text(encoding="utf-8")
	refs = re.findall(r"uses:\s+\S+@([^\s]+)", workflow)
	assert refs and all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs)
	assert "pip_audit" in workflow and "mypy" in workflow
	assert "scripts/package_windows.ps1" in workflow
	assert (ROOT / "constraints.txt").is_file()


def test_repository_metadata_and_historical_plans_are_complete():
	config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
	assert config["license"] == "MIT"
	assert config["authors"][0]["name"] == "Wilson Omesiete"
	assert config["urls"]["Repository"] == "https://github.com/womesiete/gmail-inbox-cleaner"
	assert (ROOT / "LICENSE").is_file() and (ROOT / "CHANGELOG.md").is_file()
	for name in ("implementation-plan.txt", "reliability-plan.txt", "reported-issues.txt", "reliability-plan-2.txt"):
		assert (ROOT / "docs/history" / name).is_file()
		assert not (ROOT / "docs" / name).exists()


def test_generated_validation_excludes_test_failure_content(tmp_path):
	from scripts.validation_report import render
	junit = tmp_path / "results.xml"
	junit.write_text('<testsuites><testsuite tests="4" failures="1" errors="0" skipped="0" time="1.25"><testcase><failure>PRIVATE EMAIL CONTENT</failure></testcase></testsuite></testsuites>')
	coverage = tmp_path / "coverage.xml"
	coverage.write_text('<coverage line-rate="0.82"/>')
	text = render(junit, coverage, status="failure", revision="abc123")
	assert "82.00%" in text and "4" in text and "failure" in text
	assert "PRIVATE EMAIL CONTENT" not in text


def test_setup_and_historical_reports_match_current_operations():
	setup = (ROOT / "docs/setup.md").read_text(encoding="utf-8")
	installs = [line for line in setup.splitlines() if "-m pip install" in line and "-e " in line]
	assert installs and all("-c constraints.txt" in line for line in installs)
	assert "**Disconnect**" in setup and "**Disconnect and revoke**" in setup
	assert "Local databases and backups remain" in setup
	history = (ROOT / "docs/history/reported-issues.txt").read_text(encoding="utf-8")
	assert history.startswith("HISTORICAL ISSUE REPORT — RESOLVED")
	assert "docs/reliability-fixes.md" in history
	assert (ROOT / "docs/reliability-fixes.md").is_file()
