# Changelog

## Unreleased

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
