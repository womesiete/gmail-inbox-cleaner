# Gmail Inbox Cleaner

A cross-platform desktop application for Windows, macOS, and Linux for turning a personal Gmail inbox into a list of unfinished work. It keeps lasting importance separate from the need for inbox attention: a completed insurance record can be important and still be archived, while an ordinary request awaiting a reply needs attention.

The first run is a **simulation**. The application records proposals for inspection before live execution is enabled. Its only forward Gmail writes are removing `INBOX` from explicit eligible message IDs and adding approved, existing user labels. Keeping and deferring are local decisions. Archiving retains email in Gmail and does not free storage.

## Try it without credentials

Install Python 3.11. In PowerShell from this directory:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c constraints.txt -e '.[dev]'
.\.venv\Scripts\python.exe -m gmail_clean --demo
```

The offline demo has a separate synthetic account and state. It includes newsletters, receipts, completed important records, outstanding requests, protective labels, and mixed-age conversations. No Gmail account, API key, or private AI module is needed.

```powershell
# Exercise the complete local pipeline without opening a window.
.\.venv\Scripts\python.exe -m gmail_clean --demo --headless --count 3000
# Generate and measure a 50,000-message local workload.
.\.venv\Scripts\python.exe -m gmail_clean --demo --benchmark 50000
# Launch without a console window.
.\.venv\Scripts\pythonw.exe -m gmail_clean --demo
```

The installed launchers are `gmail-clean` and `gmail-clean-gui`; the latter uses the Windows GUI entry point. You can also run `scripts/launch_windows.pyw` with the virtual environment's `pythonw.exe`. See [Windows setup](docs/setup.md) for PyCharm, Gmail OAuth, and optional packaging.

## How cleanup works

1. Configure the account, examination window, protections, and external AI module, or choose the offline demo.
2. Start a job. Its anchor and exact examination endpoints are frozen for resumability.
3. Inspect **Review** and **Plan / Simulation**. Review decisions apply to displayed, explicit message IDs.
4. Keep simulation enabled while inspecting the proposals. Enable live execution deliberately when ready.
5. Inspect receipts and conflicts in **History / Undo**. Undo reverses applicable app-owned changes while preserving unrelated later state.

By default there is no earliest date. The newest 14 days are excluded from examination, based on Gmail `internalDate`; this is independent of the **two calendar month** archival-age policy. Incomplete, contradictory, or uncertain evidence goes to review. A reference label does not prove completion. New maintenance runs discover quiet messages crossing time boundaries even without mailbox history events.

The application runs when you launch it; it does not install a background service, cloud job, or Windows scheduled task.

## Documentation

- [Windows, PyCharm, OAuth, and AI setup](docs/setup.md)
- [Review workflow, settings, and troubleshooting](docs/user-guide.md)
- [Architecture, persistence, recovery, and security](docs/architecture.md)
- [Acceptance coverage and validation commands](docs/acceptance.md)
- [Recorded test and scale-check results](docs/validation.md)
- [Reliability and performance fixes: all review rounds](docs/reliability-fixes.md)
- [Integration boundary and phased lint/type review](docs/integration-boundaries.md)
- [Changelog](CHANGELOG.md)
- [Original implementation requirements](docs/history/implementation-plan.txt)

## Development checks

```powershell
.\.venv\Scripts\python.exe -m pip install -c constraints.txt -e '.[dev]'
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe scripts/ci_tests.py
.\.venv\Scripts\python.exe -m ruff check gmail_clean tests scripts
.\.venv\Scripts\python.exe -m compileall -q gmail_clean scripts
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pip_audit --requirement constraints.txt --no-deps --disable-pip
.\.venv\Scripts\python.exe -m build
```

Linux/macOS use `.venv/bin/python` and `QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/ci_tests.py` for the combined coverage gate. Tests use synthetic messages, temporary databases, and fake transports; they require no Gmail credentials or provider access. CI defines Windows and Linux jobs; a workflow definition is not a claim that hosted CI or Windows packaging has already passed.

Google OAuth approval and the user's private AI module are deployment dependencies. The live Gmail adapter and isolated module adapter are separate from the synthetic demo; account access and real provider behavior require testing on the configured PC.

Current [validation record](docs/validation.md) and [quota measurements](docs/quota-validation.md) cover the latest follow-up. Earlier [offline timing measurements](docs/performance-2026-10-28.md) remain historical evidence.
