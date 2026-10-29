"""A real spawn worker contributes coverage to the combined application data."""
import inspect
import os
from pathlib import Path
import subprocess
import sys

from coverage import CoverageData

from gmail_clean import ai


def test_spawned_ai_worker_lines_are_recorded_and_combined(tmp_path):
	root = Path(__file__).resolve().parents[1]
	script = tmp_path / "covered_worker.py"
	script.write_text("from gmail_clean.ai import AIClient\n\nif __name__ == '__main__':\n\twith AIClient('gmail_clean.demo_ai') as client:\n\t\tassert client.call('{\"messages\": []}').text\n")
	environment = {**os.environ, "COVERAGE_FILE": str(tmp_path / ".coverage")}
	for command in (["run", "--rcfile=" + str(root / "pyproject.toml"), str(script)],
		["combine", "--rcfile=" + str(root / "pyproject.toml")]):
		result = subprocess.run([sys.executable, "-m", "coverage", *command], cwd=root,
			env=environment, capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stdout + result.stderr
	data = CoverageData(basename=str(tmp_path / ".coverage"))
	data.read()
	filename = next(name for name in data.measured_files() if name.replace("\\", "/").endswith("gmail_clean/ai.py"))
	lines, start = inspect.getsourcelines(ai._worker_main)
	module_load = next(start + index for index, line in enumerate(lines) if "module = _load_module(" in line)
	assert module_load in data.lines(filename)
