"""Generate a content-free validation record from CI summary artifacts."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import os
from pathlib import Path
import platform
import xml.etree.ElementTree as ET


def render(junit: Path, coverage: Path, *, status: str, revision: str) -> str:
	"""Read counts only; failure traces and mail/provider payloads are excluded."""
	counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
	elapsed = 0.0
	if junit.is_file():
		root = ET.parse(junit).getroot()  # noqa: S314 - local pytest-generated summary only, never mailbox input
		suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
		for suite in suites:
			for key in counts:
				counts[key] += int(suite.get(key, 0))
			elapsed += float(suite.get("time", 0))
	coverage_text = "Unavailable"
	if coverage.is_file():
		root = ET.parse(coverage).getroot()  # noqa: S314 - local coverage-generated summary only
		coverage_text = f"{float(root.get('line-rate', 0)) * 100:.2f}%"
	return f"""# Generated validation record

Generated on {datetime.now(UTC).date().isoformat()} by `scripts/validation_report.py` from JUnit and coverage summaries. CI regenerates this file and publishes it as a per-platform artifact. Historical manual measurements are retained in [the implementation record](history/validation-2026-10-28.md).

| Field | Result |
| --- | --- |
| Revision | `{revision}` |
| Environment | {platform.system()} / Python {platform.python_version()} |
| Overall check status | {status} |
| Tests collected | {counts['tests']} |
| Failures / errors / skipped | {counts['failures']} / {counts['errors']} / {counts['skipped']} |
| Pytest elapsed seconds | {elapsed:.2f} |
| Application line coverage | {coverage_text} (gate: 75%) |

The workflow also checks compilation, expanded Ruff rules, typed core modules, the offline pipeline, source/wheel builds, dependency auditing, and a Windows frozen-executable smoke test. The status reflects the generating job; consult the linked Actions run for individual check results. These tests use synthetic mail and fixture transports. No live Gmail or private AI performance is claimed. This report intentionally excludes test failure text, mail, settings, and credentials.
"""


def main(argv=None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--junit", type=Path, default=Path("test-results.xml"))
	parser.add_argument("--coverage", type=Path, default=Path("coverage.xml"))
	parser.add_argument("--output", type=Path, default=Path("docs/validation.md"))
	parser.add_argument("--status", default=os.environ.get("VALIDATION_STATUS", "local checks"))
	parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA", "local working tree"))
	args = parser.parse_args(argv)
	args.output.parent.mkdir(parents=True, exist_ok=True)
	args.output.write_text(render(args.junit, args.coverage, status=args.status, revision=args.revision), encoding="utf-8")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
