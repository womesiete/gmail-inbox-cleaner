# Changelog

## Unreleased

### Reliability follow-up

1. Resolve secure keyring chainer members, support libsecret, and report the selected backend.
2. Keep transport and temporary token-refresh failures retryable; retain invalid-grant authentication handling.
3. Distinguish unsent writes from uncertain requests and validate labels before creating attempts.
4. Preserve work through outages, quarantine repeating auth/permission items, and reject unsafe retry deadlines.
5. Recover adaptive batch sizes after clean batches and clear job-local throttling on completion.
6. Interrupt quota waits when a worker stops or the application closes.
7. Share synchronized credentials while keeping per-worker Gmail transports and fencing obsolete sessions.
8. Support provider exception-classification hooks and preserve usable or invalid retry-delay metadata.
9. Reuse history-validated job metadata and record synthetic quota consumption by stage.
10. Add a persistent quota budget, invocation-only CLI override, consumed units, and approximate ETA.
11. Separate runner and writer ownership so Apply/Undo can proceed while AI classification runs.
12. Randomize prompt boundaries and carry structured completeness flags without re-parsing prompt text.
13. Flag hidden or instruction-like content for review and isolate it from shared prompts and queued automatic execution.
14. Prefer substantive HTML over browser-view stubs and conservatively recover unclosed blocked tags.
15. Remove unused HTML sanitizers and BeautifulSoup; retain resource-blocked plain text previews.
16. Measure spawned AI workers, expand CLI/auth/provider tests, and raise the coverage gate to 85%.
17. Retain only the newest two migration snapshots by default after a successful upgrade.
18. Reconcile installation, consent revocation, resolved issue history, and operational documentation.

- Require Python 3.11 for installation and Windows/Linux CI.
- Review-bound execution hashes, write caps, explicit ownership-unverified undo, and safer failure recovery.
- Kind-aware resumable jobs, scoped retries, enforced AI budgets, and lower discovery/network overhead.
- Protected-credential recovery, revocation controls, external module integrity warnings, and security documentation.
- Expiring process-bound leases, database integrity/restore paths, bounded retention, and batched storage work.
- Redacted rotating logs, safe diagnostic export, robust legacy MIME decoding, and system timezone defaults.
- Offline crash/contract/fuzz coverage, pinned dependencies/actions, audit/type/lint gates, and Windows packaging smoke checks.

See [the complete reliability checklist](docs/reliability-fixes.md) for the numbered requirements and regression coverage. Hosted results and synthetic measurements are reported separately from live deployment checks.

## 0.1.0 — 2026-10-28

- Initial cross-platform desktop application, durable offline demo, reviewable message-scoped Gmail actions, local learning, and ownership-aware undo.
- First 18-item reliability and performance follow-up, with Windows/Linux regression coverage.
