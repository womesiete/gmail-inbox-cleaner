"""Run the complete suite and expose a bounded failure summary to Actions.

The annotation complements downloadable logs/JUnit files, including when pytest
fails before collecting tests. Failed runs remain failures; no tests are skipped.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def main(argv=None) -> int:
	arguments = sys.argv[1:] if argv is None else argv
	command = [sys.executable, "-m", "pytest", "-q", "--junitxml=test-results.xml", "--cov=gmail_clean", "--cov-report=term-missing:skip-covered", "--cov-report=xml:coverage.xml", *arguments]
	environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
	result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
		encoding="utf-8", errors="replace", env=environment, check=False)
	Path("test-output.log").write_text(result.stdout, encoding="utf-8")
	print(result.stdout, end="", flush=True)
	if result.returncode:
		# Escape workflow command data; test text cannot introduce new commands.
		detail = result.stdout[-24000:] or f"pytest exited with status {result.returncode} before producing output"
		detail = detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
		print(f"::error title=pytest failure (exit {result.returncode})::{detail}", flush=True)
	return result.returncode


if __name__ == "__main__":
	raise SystemExit(main())
