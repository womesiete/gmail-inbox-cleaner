# Reliability and performance follow-up

This change follows the supplied issue list and implementation plan. The table maps every reported item to its implementation and regression coverage.

| Issue | Resolution | Regression coverage |
| --- | --- | --- |
| 1. Stranded waiting jobs | Desktop timers resume due retries when idle; headless execution waits in interruptible slices and resumes the same frozen job. The stage runner's waiting/return contract remains intact. | `test_gui.py`, `test_lifecycle.py`, `test_jobs.py` |
| 2. Unbounded undo pagination | A 100-page limit and repeated-token detection turn incomplete ownership evidence into a conflict. | `test_actions.py` |
| 3. Batch-wide incomplete context | Target/context thread IDs and dropped-context metadata restrict uncertainty to affected conversations; unscoped context remains conservative. | `test_ai.py` |
| 4. Slow close and uninterruptible commands | Cooperative stop checks cover review, replan, execution, undo, and downloads. Closing aborts isolated AI; the three-second window deadline avoids unsafe Qt termination. Standalone process recovery is documented in architecture. | `test_gui.py`, `test_actions.py`, `test_lifecycle.py` |
| 5. GUI SQLite write contention | Active jobs receive pause/stop signals; the owning worker writes the state. Idle control writes remain local. Database close avoids an unnecessary writer lock when the connection owns no lease. | `test_gui.py`, `test_jobs.py`, `test_storage.py` |
| 6. Retained AI clients/cold starts | Exit hooks exist only for started children and are unregistered on termination/close. The application reuses serialized isolated clients across tasks while reopening worker-owned SQLite connections. | `test_ai.py`, `test_lifecycle.py` |
| 7. Demo divergence and temporary-file collisions | Synchronized demo reads detect changed file signatures; mutations use process/file locks and atomic writes. The desktop shares the synchronized demo adapter. Tokens and account identity use shared per-target reader/writer guards plus unique, fsynced temporaries with cleanup, preventing Windows destination-access races. | `test_gmail.py`, `test_service.py`, `test_lifecycle.py` |
| 8. Fragile MIME decoding | ASCII whitespace and standard Base64 alphabet are normalized before padding/strict decoding; malformed data still produces missing-content review. | `test_mime.py` |
| 9. Sequential action reads | Batched metadata prefetch/readback, phase-local thread reuse, and bounded identity checks reduce round trips while preserving fresh preflight. | `test_actions.py`, `test_security.py` |
| 10. Moving receipt endpoints | One post-write history checkpoint is shared across each execution chunk; intervening label edits remain visible to ownership checks. | `test_actions.py`, `test_security.py` |
| 11. Full inbox work on every cycle | Incremental local discovery revisits due age/defer/deadline transitions and changed semantic inputs; unchanged valid items avoid metadata retrieval. Metadata batches share thread lookups. Periodic full reconciliation remains. | `test_jobs.py`, `test_acceptance.py` |
| 12. Unstable prediction keys | Cache identity uses actual `PROMPT_VERSION` and selected prompt inputs, excluding rule statistics/timestamps. Prompt changes and changed example text invalidate reuse. | `test_jobs.py`, `test_ai.py` |
| 13. Broad invalidation/N+1 learning reads | Indexed normalized List-ID lookup, narrow feedback scopes, joined examples, metadata-only streamed template checks, and affected-pattern rebuilds replace broad scans and per-row body loads. | `test_learning.py`, `test_storage.py` |
| 14. Interactive work queued behind jobs | Rule previews and selected-text retrieval use a separate thread pool. Both pools obey shutdown and retain worker-owned resources until safe cleanup. | `test_gui.py` |
| 15. Weak inferred archive patterns | Archive suggestions need a List-ID or at least four alphabetic subject characters after prefix/number normalization. Blank/numeric-only subjects do not create inferred archive rules. | `test_learning.py` |
| 16. Inconsistent domains | A shared suffix-aware matcher and equivalent SQL match exact domains/subdomains while rejecting lookalikes; applied to rules, protections, privacy, and learning. | `test_domain.py`, `test_learning.py`, `test_ai.py` |
| 17. Bleach/duplicate HTML parsing | Bleach is removed. Optional formatted previews use one BeautifulSoup allowlist pass; plain-text preview remains the default and resource loading stays blocked. | `test_gui.py`, `test_mime.py` |
| 18. Packaging/data paths/source completeness | Requests is explicit; PyInstaller collects Gmail discovery data; platform data paths respect XDG/macOS/Windows. All required service, storage, and SQL-model modules were already complete and remain included in built distributions. | `test_runtime.py`, package build/inspection, existing service/storage/GUI suites |

The 1,000-action request-count regression uses messages in one conversation and an injectable transport: 21 bulk metadata facade calls, 11 thread reads, 32 profile/identity calls, ten history interval reads, and no individual message reads. Distinct conversations still require their own context checks. Multipart request parts continue to consume Gmail quota; these figures are not production latency measurements.

Date windows, protections, explicit message-ID scope, uncertain-write reconciliation, and ownership-aware undo remain enforced. Stage-boundary metadata checks are retained because a paused job can encounter new inbox, label, or conversation state. Performance improvements do not make Gmail reads/writes atomic.

## Second reliability review: 28-item plan

The supplied [second plan](history/reliability-plan-2.txt) is retained verbatim. Each numbered item has its own implementation commit. Optional replied-correspondent protection is not required by this pass; SENT context remains governed by the configured privacy controls. Regression checks use synthetic mail only.

| Item | Behavior / deliverable | Verification |
| --- | --- | --- |
| 1 | Switching simulation preserves reviewed proposals; explicit execution binds count and plan hash. | Action, service and GUI execution regressions. |
| 2 | Classification acknowledges each saved item independently; later failure does not requeue completed rows. | Mid-batch save failure regression. |
| 3 | Error kinds choose auth/account blocks, quota pause, job backoff, or terminal data failure; prior decisions survive transient errors. | Kind-specific job regressions. |
| 4 | Stored system pause reasons and bounded recovery distinguish temporary failures from user pause. | Discovery recovery and headless/GUI resume regressions. |
| 5 | Retry targets a job/selection, resets attempts, and preserves user pause. | Scoped retry regressions. |
| 6 | Definite write rejection uses readback and batch isolation; recovery reads messages in batches. | Definite/uncertain outcome and missing-ID regressions. |
| 7 | Per-run write caps and explicit over-limit approval bound execution; live CLI settings stay ephemeral. | CLI and execution cap regressions. |
| 8 | Undo shares indexed history evidence and offers separately confirmed unverified ownership only for an exact current after-state. | History-count, retention and label conflict regressions. |
| 9 | Unchanged credentials avoid saves; rotated refresh persistence is non-fatal and backend selection has one source of truth. | Fake keyring, DPAPI and refresh tests. |
| 10 | Disconnect-and-revoke is explicit; invalid grants explain test-mode grant expiration. | Offline revocation/error tests. |
| 11 | External Python configuration records source hashes and surfaces changed/writable code; threat model documents unencrypted local data. | Module trust regressions and `SECURITY.md`. |
| 12 | Process identity, expiry and heartbeat protect leases; break-lease requires confirmation. | Process/lease and explicit-control regressions. |
| 13 | Batch transactions and version-gated trigger creation reduce commits; checkpointing and full-job scale measurement cover persistence overhead. | Transaction counts and 50,000-message job benchmark. |
| 14 | Numbered backed-up migration removes duplicate body JSON; bounded retention includes example bodies and old queue/error/prediction rows. | Migration and chunked retention tests. |
| 15 | Backup closes and verifies its connection; restore and startup integrity checks report corruption safely. | Backup, restore and corrupt-database tests. |
| 16 | Shared bound-placeholder/chunk helpers keep large selections within SQLite limits; interpolated internal SQL has reviewed explanations. | Large-selection regressions and security lint. |
| 17 | Identity mtime, labels TTL and rules revisions avoid repeated hot-path work; replanning batches local reads. | Cache invalidation and lookup-count tests. |
| 18 | Batched target/context retrieval, quota-unit pacing and bounded jittered idempotent retries reduce network waste; page expiry matching is narrow. | Fake transport counts and error/backoff tests; [quota sources](network-performance.md). |
| 19 | Reconciliation with valid history fetches metadata for newly listed IDs; expired history triggers a complete refresh. | Unchanged inbox and expired-history regressions. |
| 20 | Per-job/daily AI call and estimated-cost caps pause before exceeding limits; sibling context reuse is measured. | AI spending and context-hash regressions. |
| 21 | Rotating level-aware logs contain structural IDs only; safe diagnostic ZIPs omit mail, settings and credentials. | `test_diagnostics.py` and GUI controls. |
| 22 | Legacy charset fallback preserves decoded text, blocked unfinished HTML is truncated, padding is stripped, and new preferences use the system zone. | `test_mime.py`, explicit DST boundaries and timezone defaults. |
| 23 | Subprocess exits exercise the durable write boundaries and recovery outcomes. | Crash-recovery subprocess tests. |
| 24 | The same protocol contract runs against demo and Google HttpMockSequence; seeded parser fuzzing and protected-storage fakes complement a 75% coverage gate. | `test_mailbox_contract.py`, `test_fuzz_contracts.py`, `test_auth_fakes.py`, coverage XML. |
| 25 | Actions are SHA-pinned; constraints, dependency audit, typed-core checks, expanded Ruff rules and Windows frozen smoke run in CI. | `test_release_policy.py`, [boundary review](integration-boundaries.md), CI checks. |
| 26 | MIT license/author/URLs, cross-platform README, changelog, historical plans and generated CI validation records complete release metadata. | Metadata/report-generation regression and source/wheel inspection. |
| 27 | GUI modules and per-stage job handlers are split after characterization; shared exclusion/history helpers retain behavior. | Characterization and full-suite regressions. |
| 28 | Final suite, lint, package build, fresh 3,000-message demo and 50,000-message measurements record implementation evidence. | [Final checks and before/after measurements](performance-2026-10-28.md). |

CI-generated [validation](validation.md) reports counts/status without failure payloads. Earlier manual measurements remain in [history](history/validation-2026-10-28.md). Synthetic timings and request/query counts are not live Gmail performance or AI-billing guarantees. Native frozen packaging results are claimed only when the corresponding hosted job passes.
