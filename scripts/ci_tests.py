"""Run the complete suite and expose a bounded failure summary to Actions.

The annotation complements downloadable logs/JUnit files, including when pytest
fails before collecting tests. Failed runs remain failures; no tests are skipped.
"""

from __future__ import annotations

import os
import io
from pathlib import Path
import subprocess
import sys


def combined_coverage() -> tuple[str, bool]:
	"""Include spawned workers that flushed after pytest's own final report.

	Each process writes a parallel data file. The wrapper runs only after pytest
	and its children exit, so the coverage gate and XML use the combined result.
	"""
	from coverage import Coverage
	from coverage.exceptions import CoverageException
	output = io.StringIO()
	try:
		coverage = Coverage(config_file="pyproject.toml")
		coverage.load()
		coverage.combine(strict=False)
		coverage.save()
		total = coverage.report(file=output, skip_covered=True)
		coverage.xml_report(outfile="coverage.xml")
		required = coverage.config.fail_under
		passed = total >= required
		output.write(f"Combined application coverage: {total:.2f}% (required: {required:g}%)\n")
		return output.getvalue(), passed
	except CoverageException:
		return "Coverage collection failed; inspect the test diagnostics and parallel data files.\n", False


def main(argv=None) -> int:
	arguments = sys.argv[1:] if argv is None else argv
	command = [sys.executable, "-m", "pytest", "-q", "--junitxml=test-results.xml", "--cov=gmail_clean", "--cov-report=", "--cov-fail-under=0", *arguments]
	environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
	result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,  # noqa: S603 - fixed Python executable runs pytest with explicit local command-line arguments
		encoding="utf-8", errors="replace", env=environment, check=False)
	coverage_output, coverage_passed = combined_coverage()
	output = result.stdout + coverage_output
	returncode = result.returncode or (0 if coverage_passed else 1)
	Path("test-output.log").write_text(output, encoding="utf-8")
	print(output, end="", flush=True)
	if returncode:
		# Escape workflow command data; test text cannot introduce new commands.
		detail = output[-24000:] or f"pytest exited with status {returncode} before producing output"
		detail = detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
		print(f"::error title=pytest failure (exit {returncode})::{detail}", flush=True)
	return returncode


if __name__ == "__main__":
	raise SystemExit(main())
