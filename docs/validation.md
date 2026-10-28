# Validation record

Checks recorded on **2026-10-28**, using **Linux and Python 3.12** in a project virtual environment. Qt checks used an offscreen environment. These measurements describe the local synthetic workload on the implementation host; they are not Gmail/provider performance claims or measurements on the target Windows PC.

## Checks executed

| Check | Result |
| --- | --- |
| Editable installation, `python -m pip install -e '.[dev]'` | Passed. |
| Complete suite, `QT_QPA_PLATFORM=offscreen python -m pytest -q` | 211 tests passed in 14.83 seconds, including final privacy, action ownership, GUI, and console opt-in regressions. |
| Cross-component offline acceptance, `python -m pytest tests/test_acceptance.py -q` | 4 passed in 1.94 seconds. |
| Full lint, `python -m ruff check gmail_clean tests scripts` | Passed with Ruff 0.9.10. |
| Compile checks, `python -m compileall -q gmail_clean tests scripts` | Passed. |
| Dependency consistency, `python -m pip check` | Passed; no broken requirements. |
| Source/wheel packaging, `python -m build` | Passed; produced an sdist with documentation/scripts/tests and a wheel with application modules. |

The [acceptance checklist](acceptance.md) maps requirement areas to specific test files. The GitHub Actions workflow defines Linux/Windows checks; creating that workflow does not establish that hosted CI or Windows-native execution has passed.

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
