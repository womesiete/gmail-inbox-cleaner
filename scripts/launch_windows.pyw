"""Double-click launcher for the project's Windows virtual environment.

The console-free entry point installed by pip is also available as
.venv\\Scripts\\gmail-clean-gui.exe. No Gmail credentials are embedded here.
"""
from __future__ import annotations

import multiprocessing
from pathlib import Path
import subprocess
import sys


def launch() -> int:
	project = Path(__file__).resolve().parents[1]
	pythonw = project / ".venv" / "Scripts" / "pythonw.exe"
	if pythonw.exists() and Path(sys.executable).resolve() != pythonw.resolve():
		subprocess.Popen([str(pythonw), "-m", "gmail_clean", *sys.argv[1:]], cwd=project)
		return 0
	from gmail_clean.app import main
	return main()


if __name__ == "__main__":
	multiprocessing.freeze_support()
	raise SystemExit(launch())
