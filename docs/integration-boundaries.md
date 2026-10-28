# Integration boundary and lint review

The expanded Ruff gate enables B, BLE, S and PERF for production code. Tests retain assertion, synthetic-secret, seeded-fuzz and fixed-subprocess exemptions. Every production broad catch below was reviewed as an explicit integration, cleanup or durable-stage boundary. It sanitizes an error, records per-item failure, preserves an uncertain write, or cleans up and re-raises. New blanket catches remain lint failures unless justified at their call site. SQL interpolation is limited to internal identifiers/fragments and placeholder counts; message IDs and user values stay bound. Qt virtual methods retain immutable QModelIndex defaults. Win32 entry points are looked up lazily because non-Windows type stubs omit their symbols.

The inventory records source locations at the item 25 integration. Later behavior-preserving module moves retain the reviewed call-site explanations.

| Catch boundary | Type | Review reason |
| --- | --- | --- |
| `actions.py:ActionExecutor._prefetch:61` | `Exception` | multipart failure is isolated before independent preflight readback |
| `actions.py:ActionExecutor._apply:300` | `Exception` | per-action preflight failures become sanitized conflicts |
| `actions.py:ActionExecutor._apply:320` | `Exception` | one final preflight failure must not abort unrelated actions |
| `actions.py:ActionExecutor._apply:386` | `Exception` | write transport failure must preserve definite or uncertain durable outcomes |
| `actions.py:ActionExecutor._apply:425` | `Exception` | a failed post-write checkpoint leaves durable attempts uncertain |
| `actions.py:ActionExecutor._apply:444` | `Exception` | post-write receipt failure must retain reconciliation rather than replay |
| `actions.py:ActionExecutor._history_interval:488` | `Exception` | unavailable ownership evidence must fail closed for undo |
| `ai.py:_load_module:377` | `BaseException` | Roll back module registration even for SystemExit, then re-raise. |
| `ai.py:_worker_main:396` | `BaseException` | Untrusted module initialization may raise SystemExit; report safe child failure. |
| `ai.py:_worker_main:437` | `Exception` | Provider exception identities are classified and raw content is withheld. |
| `ai.py:AIClient._ensure_worker:518` | `BaseException` | Clean up IPC if spawn is interrupted, then preserve the original failure. |
| `app.py:main:187` | `Exception` | standalone repair boundary emits sanitized startup failure only |
| `app.py:main:289` | `Exception` | top-level process boundary emits a sanitized startup diagnostic |
| `app.py:main:301` | `Exception` | top-level process boundary emits a sanitized startup diagnostic |
| `auth.py:TokenStore.save:115` | `Exception` | OS keyring backends expose different exception types. |
| `auth.py:TokenStore.save:119` | `Exception` | Preserve the original keyring failure when encrypted fallback fails. |
| `auth.py:TokenStore.load:153` | `Exception` | Third-party keyring failures stay behind the protected-store boundary. |
| `auth.py:TokenStore.delete:175` | `Exception` | Backend failures must still remove the encrypted-file copy. |
| `auth.py:AuthManager.connect:224` | `Exception` | OAuth/provider errors need a content-free user-facing summary. |
| `auth.py:AuthManager.restore:242` | `Exception` | A credential persistence failure cannot invalidate the refreshed in-memory session. |
| `auth.py:AuthManager.restore:255` | `Exception` | Unexpected credential errors cannot expose token/response details. |
| `demo.py:DemoMailbox._save:277` | `Exception` | Roll back or close owned resources, then re-raise the original exception. |
| `demo.py:DemoMailbox.get_messages:359` | `Exception` | per-item mailbox protocol returns independent failures |
| `gmail.py:GmailMailbox._execute:170` | `Exception` | transport/decode boundaries normalize arbitrary integration exceptions. |
| `gmail.py:GmailMailbox.get_messages:261` | `Exception` | transport/decode boundaries normalize arbitrary integration exceptions. |
| `gmail.py:GmailMailbox.get_messages:272` | `Exception` | transport/decode boundaries normalize arbitrary integration exceptions. |
| `gui.py:ServiceTask.run:194` | `Exception` | Roll back or close owned resources, then re-raise the original exception. |
| `gui.py:ServiceTask.run:198` | `Exception` | secondary reporting failure must not mask the original task error; secondary error reporting must preserve the original task exception |
| `gui.py:ServiceTask.run:202` | `Exception` | Qt worker boundary emits only a sanitized failure signal |
| `jobs.py:JobRunner.run:218` | `Exception` | job boundary persists a kind-aware resumable pause |
| `jobs.py:JobRunner.discover:238` | `Exception` | Roll back or close owned resources, then re-raise the original exception. |
| `jobs.py:JobRunner.discover:277` | `Exception` | Roll back or close owned resources, then re-raise the original exception. |
| `jobs.py:JobRunner.discover:302` | `Exception` | Roll back or close owned resources, then re-raise the original exception. |
| `jobs.py:JobRunner.sync_history:332` | `Exception` | Roll back or close owned resources, then re-raise the original exception. |
| `jobs.py:JobRunner._prefetch_threads:372` | `Exception` | thread transport failures are returned as per-thread evidence |
| `jobs.py:JobRunner.process_stage:413` | `Exception` | metadata transport failure is assigned to unresolved work only |
| `jobs.py:JobRunner.process_stage:469` | `Exception` | per-item safety check failure enters the kind-aware work queue |
| `jobs.py:JobRunner.process_stage:494` | `Exception` | multipart failure produces independent per-item error results |
| `jobs.py:JobRunner.process_stage:523` | `Exception` | one persistence failure must not discard prior completed rows |
| `jobs.py:JobRunner.classify:636` | `Exception` | classification metadata transport failure remains per-item evidence |
| `jobs.py:JobRunner.classify:669` | `Exception` | one classification preflight failure must preserve other targets |
| `jobs.py:JobRunner.classify:710` | `Exception` | provider group failure enters kind-aware job backoff without losing receipts |
| `jobs.py:JobRunner.classify:724` | `Exception` | save failure retries only the unresolved classification item |
| `learning.py:Learning.batch:54` | `BaseException` | Roll back or close owned resources, then re-raise the original exception. |
| `mime.py:parse_body.walk:130` | `Exception` | attachment callback boundary marks unavailable content without leaking errors |
| `service.py:AppService.run_job:403` | `Exception` | label discovery failure becomes a stored system pause before job entry |
| `storage.py:Store.__init__:267` | `Exception` | Roll back or close owned resources, then re-raise the original exception. |
| `storage.py:Store._migrate:305` | `Exception` | Roll back or close owned resources, then re-raise the original exception. |
| `storage.py:Store.transaction:381` | `BaseException` | Roll back or close owned resources, then re-raise the original exception. |
| `storage.py:Store.lease_heartbeat.renew:524` | `Exception` | heartbeat thread relays lease loss to its owner for safe cleanup |

Mypy gates all 16 non-Qt package modules with `check_untyped_defs` enabled. GUI modules, the SQL Qt table model, and the application entry point that constructs Qt are excluded; this does not claim complete GUI type coverage. Dependency constraints cover the validated application/test/audit/package environment and include Windows-only frozen-build dependencies. Audit failures fail CI; no vulnerabilities are silently ignored. GitHub action pins were resolved from official repository tag references on 2026-10-28.
