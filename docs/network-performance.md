# Network work and quota accounting

Service batches reuse account-file state until its stat signature changes, share a 60-second label catalog across workers, and cache enabled rules by the stored `rules_revision`. Explicit label failures invalidate the catalog. Action preflight still checks current account identity and message state.

Offline request-count regressions compare three selected messages in one conversation:

| Operation | Previous service calls | Current service calls |
| --- | --- | --- |
| Manual review preflight | Three single-message reads and three thread reads | One bulk metadata read and one thread read |
| Body download | Six single-message reads and three thread reads | Two bulk reads and one thread read |
| Three optional context bodies, repeated within one phase | Six body reads and two thread reads | One body batch and one thread read |

These are facade calls, not Gmail quota savings: every multipart part is charged separately. Cache lifetimes stop at the operation or stage boundary so subsequent preflights can see changed conversations. Privacy and frozen/current date bounds are checked before context body retrieval.

Quota accounting was checked against [Google's official schedule](https://developers.google.com/workspace/gmail/api/reference/quota) on 2026-10-28. The conservative shared budget is 6,000 units per user per minute, using metadata/full message gets at 20/60 units, metadata thread gets at 40, attachments at 20, batch modification at 50, message listing at five, history at two, and profile/label listing at one. Projects used before the May 2026 change can retain older quotas. Local accounting cannot see other applications consuming the same project quota; Gmail's Retry-After still applies.

Idempotent reads have at most two local retries with exponential jitter. A Retry-After longer than 60 seconds is returned to the durable job scheduler. Label writes are never automatically retried by the transport because the executor must reconcile their outcome first.
