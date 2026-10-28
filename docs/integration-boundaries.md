# Integration boundary and lint review

The expanded Ruff gate enables B, BLE, S, and PERF across production code. Tests retain narrow assertion, synthetic-secret, seeded-fuzz, and fixed-subprocess exemptions. Broad integration catches sanitize/report failures, preserve uncertain outcomes, or complete cleanup before re-raising. Each intentional exception has a reviewed call-site explanation; new unexplained catches fail lint. SQL interpolation uses internal identifiers/fragments and generated placeholders while message IDs and user values remain bound. Qt virtual methods retain immutable QModelIndex defaults.

This inventory reflects the final extracted GUI modules and job handlers.

| Catch boundary | Type | Review reason |
| --- | --- | --- |
| `actions.py:ActionExecutor._prefetch` | `Exception` | multipart failure is isolated before independent preflight readback |
| `actions.py:ActionExecutor._apply` | `Exception` | per-action preflight failures become sanitized conflicts |
| `actions.py:ActionExecutor._apply` | `Exception` | one final preflight failure must not abort unrelated actions |
| `actions.py:ActionExecutor._apply` | `Exception` | write transport failure must preserve definite or uncertain durable outcomes |
| `actions.py:ActionExecutor._apply` | `Exception` | a failed post-write checkpoint leaves durable attempts uncertain |
| `actions.py:ActionExecutor._apply` | `Exception` | post-write receipt failure must retain reconciliation rather than replay |
| `actions.py:ActionExecutor._history_interval` | `Exception` | unavailable ownership evidence must fail closed for undo |
| `ai.py:_load_module` | `BaseException` | Roll back module registration even for SystemExit, then re-raise. |
| `ai.py:_worker_main` | `BaseException` | Untrusted module initialization may raise SystemExit; report safe child failure. |
| `ai.py:_worker_main` | `Exception` | Provider exception identities are classified and raw content is withheld. |
| `ai.py:AIClient._ensure_worker` | `BaseException` | Clean up IPC if spawn is interrupted, then preserve the original failure. |
| `app.py:main` | `Exception` | standalone repair boundary emits sanitized startup failure only |
| `app.py:main` | `Exception` | top-level process boundary emits a sanitized startup diagnostic |
| `app.py:main` | `Exception` | top-level process boundary emits a sanitized startup diagnostic |
| `auth.py:TokenStore.save` | `Exception` | OS keyring backends expose different exception types. |
| `auth.py:TokenStore.save` | `Exception` | Preserve the original keyring failure when encrypted fallback fails. |
| `auth.py:TokenStore.load` | `Exception` | Third-party keyring failures stay behind the protected-store boundary. |
| `auth.py:TokenStore.delete` | `Exception` | Backend failures must still remove the encrypted-file copy. |
| `auth.py:AuthManager.connect` | `Exception` | OAuth/provider errors need a content-free user-facing summary. |
| `auth.py:AuthManager.restore` | `Exception` | A credential persistence failure cannot invalidate the refreshed in-memory session. |
| `auth.py:AuthManager.restore` | `Exception` | Unexpected credential errors cannot expose token/response details. |
| `demo.py:DemoMailbox._save` | `Exception` | Cleanup or transaction rollback preserves and re-raises the original failure. |
| `demo.py:DemoMailbox.get_messages` | `Exception` | per-item mailbox protocol returns independent failures |
| `gmail.py:GmailMailbox._execute` | `Exception` | transport/decode boundaries normalize arbitrary integration exceptions. |
| `gmail.py:GmailMailbox.get_messages` | `Exception` | transport/decode boundaries normalize arbitrary integration exceptions. |
| `gmail.py:GmailMailbox.get_messages` | `Exception` | transport/decode boundaries normalize arbitrary integration exceptions. |
| `gui_workers.py:ServiceTask.run` | `Exception` | Failure reporting cannot replace the original worker exception; the outer task boundary sanitizes it. |
| `gui_workers.py:ServiceTask.run` | `Exception` | secondary reporting failure must not mask the original task error; secondary error reporting must preserve the original task exception |
| `gui_workers.py:ServiceTask.run` | `Exception` | Qt worker boundary emits only a sanitized failure signal |
| `jobs.py:JobRunner.run` | `Exception` | job boundary persists a kind-aware resumable pause |
| `jobs.py:JobRunner.discover` | `Exception` | Recognized history/page expiry triggers bounded replay or reconciliation; other failures propagate to the durable job boundary. |
| `jobs.py:JobRunner.discover` | `Exception` | Recognized history/page expiry triggers bounded replay or reconciliation; other failures propagate to the durable job boundary. |
| `jobs.py:JobRunner.discover` | `Exception` | Recognized history/page expiry triggers bounded replay or reconciliation; other failures propagate to the durable job boundary. |
| `jobs.py:JobRunner.sync_history` | `Exception` | Recognized history/page expiry triggers bounded replay or reconciliation; other failures propagate to the durable job boundary. |
| `jobs.py:JobRunner._prefetch_threads` | `Exception` | thread transport failures are returned as per-thread evidence |
| `jobs.py:JobRunner._prepare_body_batch` | `Exception` | metadata transport failure is assigned to unresolved work only |
| `jobs.py:JobRunner._prepare_body_batch` | `Exception` | per-item safety check failure enters the kind-aware work queue |
| `jobs.py:JobRunner._retrieve_stage_batch` | `Exception` | multipart failure produces independent per-item error results |
| `jobs.py:JobRunner._retrieve_stage_batch` | `Exception` | one persistence failure must not discard prior completed rows |
| `jobs.py:JobRunner._prepare_classification` | `Exception` | classification metadata transport failure remains per-item evidence |
| `jobs.py:JobRunner._classification_candidate` | `Exception` | one classification preflight failure must preserve other targets |
| `jobs.py:JobRunner._classify_group` | `Exception` | provider group failure enters kind-aware job backoff without losing receipts |
| `jobs.py:JobRunner._classify_group` | `Exception` | save failure retries only the unresolved classification item |
| `learning.py:Learning.batch` | `BaseException` | Cleanup or transaction rollback preserves and re-raises the original failure. |
| `mime.py:parse_body.walk` | `Exception` | attachment callback boundary marks unavailable content without leaking errors |
| `service.py:AppService.run_job` | `Exception` | label discovery failure becomes a stored system pause before job entry |
| `storage.py:Store.__init__` | `Exception` | Cleanup or transaction rollback preserves and re-raises the original failure. |
| `storage.py:Store._migrate` | `Exception` | Cleanup or transaction rollback preserves and re-raises the original failure. |
| `storage.py:Store.transaction` | `BaseException` | Cleanup or transaction rollback preserves and re-raises the original failure. |
| `storage.py:Store.lease_heartbeat.renew` | `Exception` | heartbeat thread relays lease loss to its owner for safe cleanup |

Mypy gates all 16 non-Qt application modules with `check_untyped_defs` enabled. GUI modules, the Qt table model, and the Qt-aware entry point are excluded. Dynamic JSON/SDK/platform boundaries use explicit types and lazy platform lookup; no blanket type ignores were added. Dependency constraints cover the validated application/test/audit/package environment, including Windows-only packaging dependencies. Audit failures fail CI; no known vulnerabilities are suppressed. GitHub Actions are pinned to commit SHAs resolved from official repository references on 2026-10-28.
