# Validation record

Checks recorded on **2026-10-28**, using **Linux and Python 3.12** in a project virtual environment. Qt checks used an offscreen environment. These measurements describe the local synthetic workload on the implementation host; they are not Gmail/provider performance claims or measurements on the target Windows PC.

## Checks executed

| Check | Result |
| --- | --- |
| Editable installation, `python -m pip install -e '.[dev]'` | Passed. |
| Complete suite, `QT_QPA_PLATFORM=offscreen python scripts/ci_tests.py` | 316 tests passed in 29.11 seconds after the 18-issue reliability/performance follow-up. |
| Cross-component offline acceptance | All four acceptance workflows passed as part of the complete suite. |
| Full lint, `python -m ruff check gmail_clean tests scripts` | Passed with Ruff 0.9.10. |
| Compile checks, `python -m compileall -q gmail_clean tests scripts` | Passed. |
| Dependency consistency, `python -m pip check` | Passed; no broken requirements. |
| Source/wheel packaging, `python -m build` | Passed; produced an sdist with documentation/scripts/tests and a wheel with application modules. |

The [acceptance checklist](../acceptance.md) maps requirement areas to specific test files. The GitHub Actions workflow defines Linux/Windows checks; creating that workflow does not establish that hosted CI or Windows-native execution has passed.

## Hosted-CI portability corrections

The first hosted run, [40697152081](https://github.com/womesiete/gmail-inbox-cleaner/actions/runs/40697152081), failed during tests on all four OS/Python combinations despite the earlier 211-test local Linux pass. Installation, compilation, and lint succeeded. Public annotations reported pytest exit code 3 on Linux and exit code 1 on Windows; they did not include the test traceback.

The follow-up correction adds:

- Query-only Windows process-liveness checks using `OpenProcess`, `GetExitCodeProcess`, and `CloseHandle`. Windows must never use POSIX-style `os.kill(pid, 0)` for lease probing. Regressions include a subprocess self-probe so unintended termination cannot look like a successful test exit.
- Explicit Linux Qt native runtime dependencies and an offscreen QApplication startup check before pytest.
- Separate AI worker readiness and active-call timeout tests, so cold Windows process startup is not mistaken for a timed-out provider request.
- A test wrapper that preserves failures, emits a bounded traceback annotation, and saves JUnit/output files. Failed hosted jobs upload these diagnostics. The wrapper's failure path was separately checked with an intentionally failing synthetic test.
- Current Node24-based versions of the checkout, Python setup, and artifact actions. The original Node20 deprecation annotations were warnings, not evidence of the test failure's cause.

The portability correction passed its 220-test local suite and [all four hosted Windows/Linux Python 3.11/3.12 jobs](https://github.com/womesiete/gmail-inbox-cleaner/actions/runs/40699101132). A local result alone does not establish hosted success.

## Reliability and performance follow-up

The [18-issue implementation checklist](../reliability-fixes.md) maps changes to their regressions. The current local suite contains 316 passing tests, including due-retry recovery, per-conversation context uncertainty, bounded undo pagination, pooled AI cleanup, blocked-network standalone closure, atomic demo writes, and rule/control race regressions.

The required fresh 60-message headless demo completed with 57 metadata candidates and 155 completed stage items: zero failed, pending, or retry items, ending at Completed / Review. The final source/wheel build and dependency check passed. Wheel inspection confirmed complete parseable `service.py`, `storage.py`, and `table_model.py` modules, an explicit Requests requirement, and no Bleach requirement. The Windows packaging script collects Gmail static discovery data; a native frozen executable has not been built on this Linux host.

Performance tests include a 1,000-action shared-conversation transport-count regression and a 50,000-row learning/template lookup regression. They measure synthetic behavior and query/request counts, not live Gmail latency or provider cost. The first hosted follow-up exposed concurrent account-file replacement failures on both Windows versions: each had 313 passing tests and one failing identity-write regression. Shared per-target reader/writer guards now serialize identity and encrypted-token operations across worker instances; deterministic simulated-Windows regressions run on every platform. Current hosted workflow results are reported separately from these local checks.

## Offline 3,000-message pipeline

Command form, using a fresh private data directory:

```bash
python -m gmail_clean --demo --headless --count 3000 --data-dir <fresh-private-base>
```

The synthetic mailbox contained 3,000 messages distributed across eligible inbox mail, recently excluded messages, read-only/non-target states, protections, and mixed-age conversations. The run reported:

| Field | Observed result |
| --- | --- |
| Durable metadata candidates | 2,834 |
| Completed stage items | 7,844 |
| Failed stage items | 0 |
| Pending stage items | 0 |
| Retry stage items | 0 |
| Job state / final stage | Completed / Review |

Stage counts include metadata, body retrieval, and classification, so they are not a count of distinct emails. The run used simulation mode and the deterministic demo AI; it required no account credentials or provider keys.

## 50,000-row SQL/model measurement

Command form:

```bash
python -m gmail_clean --demo --benchmark 50000 --data-dir <fresh-private-base>
```

| Measurement | Observed value |
| --- | --- |
| Stored/model rows | 50,000 |
| Generate and store synthetic rows | 8.7775 s |
| Model construction and first page | 0.005940 s |
| Last-page access | 0.047134 s |
| Maximum retained row pages | 8 |
| Rows per page | 128 |
| Maximum cached rows at those defaults | 1,024 |

This exercises real SQLite-backed table paging with bounded LRU caching. It does not include network retrieval, model-provider latency, a full 50,000-item classification run, or measured interactive paint/scroll latency on Windows. Re-run with a fresh directory to distinguish initial seeding from an existing database workload.

## Deployment checks still requiring configuration

No live Gmail mailbox or private external AI provider was used. Browser OAuth consent, Windows Credential Manager/DPAPI, `pythonw.exe`, PyInstaller output, and Windows-specific spawned-process behavior require a smoke test on the configured Windows machine. Those implementations and setup paths are included; their environment-dependent results are not claimed here.
