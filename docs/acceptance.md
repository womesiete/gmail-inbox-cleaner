# Acceptance and validation

This checklist connects the requested behavior to inspectable implementation and automated test areas. A passing synthetic/fake-transport test is not a claim that a real Gmail account, private provider, Windows Credential Manager, or a Windows frozen executable was exercised.

See the [validation record](validation.md) for checks and scale measurements actually run during implementation.

| Requirement area | Implementation | Automated evidence |
| --- | --- | --- |
| Unlimited earliest bound; rolling/fixed endpoints; exact milliseconds; timezone/DST; month-end clamping | `domain.py` frozen `Window`, UTC helpers, calendar age cutoff | `test_domain.py` date/window/calendar tests |
| Frozen jobs; quiet time-boundary discovery; history/page-token recovery; deduplication | `jobs.py` durable manifests/checkpoints, history replay, boundary sweeps and local inbox reconsideration | `test_jobs.py` |
| Separate attention/importance/category; completed important records; protections, overrides and conflicts | `domain.py` assessment and deterministic recommendation; service/action preflight | `test_domain.py`, `test_actions.py` |
| No excluded target bodies/AI/mutations; mixed-age thread safety; message-level targets | Metadata-first job stages, live metadata preflight before content and AI, exact local eligibility, metadata-only thread membership, explicit action IDs | `test_jobs.py`, `test_acceptance.py`, `test_security.py` |
| MIME/base64url/charsets/nested text attachments/HTML/size bounds; no mark-read | `mime.py`, `gmail.py`, resource-blocking preview | `test_mime.py`, `test_gmail.py`, `test_gui.py` |
| Exact external module contract; model omission/forwarding; load once; exported exception identity | `ai.py` spawned serial worker, `demo_ai.py` full contract | `test_ai.py` package/file imports, minimal modules, default/explicit model, exception and process lifecycle tests |
| Strict/partial schema validation; unknown labels and IDs; injection separation | `ai.py` prompt/response validation; policy-only executable decisions | `test_ai.py` malformed/partial/duplicate/unsupported-field/privacy tests |
| Separate label/attention feedback; bulk support; counterexamples; pause/reset | `learning.py`, context-bound manual overrides and separate audit ledger | `test_learning.py`, `test_acceptance.py` |
| Crash recovery, retries, unknown write outcomes and minimal deltas | Durable work items/attempts, per-message reconciliation, preflight checks | `test_jobs.py`, `test_actions.py`, `test_gmail.py` |
| Ownership-aware undo, queued cancellation, protected/deleted label conflicts | Action ledger, bounded write-interval history evidence, and original message-ID inverse deltas | `test_actions.py`, `test_security.py` |
| Account/mode separation; migrations; thread ownership; execution leases; backups | `storage.py`, runtime paths, token store, QLockFile and SQLite leases | `test_storage.py`, `test_gmail.py`, `test_gui.py`, `test_acceptance.py` |
| 50,000-row paging; sorting/filtering; safe shortcuts; multi-label selection; GUI thread updates | `table_model.py` SQL/LRU pages, `gui.py` workers and signals | `test_gui.py` and the repeatable benchmark command |
| Console writes require an invocation-specific opt-in; bulk review avoids network I/O under SQLite's writer lock | `app.py`, `service.py` | `test_app.py` |

## Repeatable local checks

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check gmail_clean tests scripts
.\.venv\Scripts\python.exe -m compileall -q gmail_clean scripts
.\.venv\Scripts\python.exe -m build
.\.venv\Scripts\python.exe -m gmail_clean --demo --headless --count 3000
.\.venv\Scripts\python.exe -m gmail_clean --demo --benchmark 50000
```

For reproducible demo initialization, use a fresh private `--data-dir` base. Existing demo mailbox files intentionally retain synthetic changes across launches; changing `--count` does not implicitly destroy that state. Record the platform, Python version, message count, workload, and reported timings with any benchmark result. A database/table benchmark does not measure Gmail latency, provider throughput, or human interface latency on another PC.

## Environment-dependent checks

The following require the deployment environment and user-supplied configuration:

- Browser OAuth consent, token refresh/revocation, and verification/account policy behavior with the intended personal Gmail account.
- Actual Windows Credential Manager/DPAPI storage, folder ACLs, and Windows console-free launch.
- The private AI module's import dependencies, actual model identifiers, credentials, routing, billing, and remote cancellation behavior.
- Optional PyInstaller packaging and spawned-worker smoke tests on the target Windows PC.

No production Gmail mailbox was changed by the repository's offline test suite. Demo mode cannot use real Gmail credentials or mutate a real account. Read the [setup instructions](setup.md) before testing the live adapter.
