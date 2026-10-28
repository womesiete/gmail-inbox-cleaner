# Using Gmail Inbox Cleaner

## Simulation and live actions

Simulation is enabled by default. A job can discover messages, assess them, save feedback, and build an action ledger without changing Gmail. **Queued/proposed** does not mean **applied**. Inspect the target counts, exact dates, reasons, and exceptions in Plan before switching off simulation. Switching simulation off preserves the reviewed plan. Commands submitted while Simulation is on remain read-only for their entire worker invocation, even if newer preferences enable live mode while they wait; the next live command requires a fresh explicit choice. Choose **Execute this plan** (or **Execute selected plan**) and confirm its exact action count and SHA-256 plan hash. If target IDs or write intents changed while the confirmation was open, execution refuses the old approval and requires another review.

In live mode, an ordinary manual Archive or label command queues execution asynchronously; repeated confirmation dialogs are not part of the normal review flow. Direct selections apply only to the displayed explicit message IDs. The live executor still checks the account, scope, current protections, labels, context, and action versions. A setting change or new reply can make an approved proposal stale.

The application never deletes or trashes messages, sends mail, executes attachments, unsubscribes, or creates Gmail filters. It archives by removing `INBOX`. Existing unread state, stars, importance, and unrelated labels are preserved. Keep and Defer do not invoke Gmail Snooze or change mailbox labels.

## First run and maintenance

Start in the offline demo to learn the workflow. In real mode, connect the intended Gmail account and configure the private AI module. Review the frozen job interval and independent age cutoff before starting.

A new run creates a new job anchor. Pause prevents new work and preserves progress. Resume uses the original anchor, candidate manifest, and stage checkpoints. Stop also preserves completed work; it is not a rollback. Failures stay visible in the error queue and can be retried after their cause is fixed.

Maintenance combines mailbox history with a rolling boundary sweep, local deadline/defer/age reevaluation, and periodic inbox reconciliation. This discovers a quiet message that becomes 14 days old even if it has no history event. The application does no processing while closed. Overview offers an optional **run maintenance while open** checkbox, initially off; enabling it uses the maintenance interval without installing a background task.

## Review workflow

Use Review to inspect sender, subject, date, current/suggested labels, category, lasting importance, outstanding action, uncertainty, and the reason for the recommendation. Preview content is sanitized and does not load external resources or mark the Gmail message read. Open in Gmail is an explicit navigation action using the connected account context; the browser may still prompt you to select/sign into that account.

- **Archive:** This item no longer needs inbox attention. Optional Completed, Expired, or Not relevant reasons preserve that distinction. Archival is not a judgment that future mail from the sender lacks importance.
- **Keep / Needs attention:** Retain the current item/context. This does not create a permanent sender-wide protection.
- **Defer:** Store a local defer-until instant. The item returns when due without requiring Gmail history. Meaningful new conversation context invalidates the old defer.
- **Labels:** Select multiple existing user labels. Adding a label is a positive category example; other labels are not automatically treated as wrong. Rejecting an unapplied suggestion changes the proposal/feedback, not unrelated preexisting Gmail labels.
- **Undo:** Cancel a queued proposal or reverse applicable executed app-owned changes. Conflicts are shown rather than overwritten.

Group selection is a convenience for reviewing explicit items. Inspect count, date range, representative examples, and exceptions. An entire thread is never a Gmail mutation target. A newer excluded reply can make old conversation conclusions unsafe; the recent body remains outside classification and its message ID is never silently added to an approved action.

Ordinary feedback defaults to this message or the currently displayed selection. Choose an explicit recurring pattern or sender-plus-condition scope only when intending a reusable rule. A broad inferred archive rule needs explicit approval. Initial shortcuts are **A** Archive, **K** Keep, **D** Defer, **L** Add labels, **O** Open Gmail, and **Ctrl+Z** Undo last action. Shortcut settings are editable and persisted; shortcuts are suppressed while typing in editors.

## Independent date controls

| Setting | Default | Exact meaning |
| --- | --- | --- |
| Earliest date | Unset / unlimited | Optional local calendar date; its midnight is inclusive. No lower-bound search clause is emitted when unset. |
| Latest mode | Rolling | Exclusive endpoint is the job's UTC anchor minus a configured number of exact 24-hour days. |
| Latest rolling days | 14 | Excludes the latest two weeks; zero is allowed. |
| Fixed latest date | Unset | In fixed mode, the entire selected local date is included; the following local midnight is the exclusive endpoint. |
| Calendar/display timezone | America/New_York | Determines calendar endpoints and month arithmetic; stored timestamps remain UTC. |
| Age archival enabled | Yes | Applies to assessed eligible mail with established no outstanding action, subject to protections and uncertainty rules. |
| Global age | 2 calendar months | Subtract months in the configured timezone, clamp the day to the month's end, preserve local time, then convert to UTC. This is not 60 days. |
| Category age overrides | None | Inherit the global policy, use a category-specific month/day threshold, or disable age-based archival for that category. Conflicting applicable overrides require review. |

Eligibility is `earliest <= internalDate < latest`, using Gmail's millisecond `internalDate`. The sender's Date header is not the age source. Gmail epoch-second query bounds deliberately overfetch; the exact checks happen locally. Malformed dates and empty/inverted windows are rejected.

Age means **strictly earlier** than the resolved cutoff. Calendar-month subtraction resolves an ambiguous fall-back wall time with `fold=0`; a nonexistent spring-forward time advances to its post-transition instant. The job preview displays concrete UTC endpoints and timezone. Tightening current scope/protections constrains an existing frozen job too; loosening settings does not expand its manifest.

## Recommendations and protections

Policy keeps category, importance, urgency, outstanding action, and need for inbox attention separate. Ineligibility, configured protections, applicable direct decisions, and approved rules are evaluated before score-based defaults. A reference/filed label does not prove completion; requires-attention/protected labels can prevent automatic archival.

The binding conservative AI policy routes any non-`none` outstanding-action value, non-low uncertainty, missing context, truncated/unparsed body, or contradictory signals to Review. For example, `outstanding_action=none` with a score at/above the Keep cutoff is contradictory and needs review. This preserves unresolved work even when the message has a reference label or is old.

For complete, consistent assessments with no outstanding action:

| Condition | Recommendation |
| --- | --- |
| Applicable active age policy; older than cutoff; score below 65 | Archive |
| Applicable age policy; older than cutoff; score 65–69 | Review |
| No applicable old-age decision; score below 25 | Archive |
| No applicable old-age decision; score 25–69 | Review |
| Score at/above 70 with contradictory no-action evidence | Review |

Thresholds are adjustable policy defaults, not calibrated probabilities. Deterministic approved decisions can have a different evidence basis from a model's self-reported confidence. No assessment means no automatic archive merely because a protection was absent. Important completed mail can archive without an existing user label.

Manual actions can explicitly override protections shown for their displayed selection. The ledger records that bypass; a newly introduced or previously undisplayed protection invalidates it during preflight. Rule conflicts remain visible. Changing thresholds recomputes policy recommendations from valid stored assessments; it does not automatically unarchive already executed actions.

## Preferences

Preferences are stored with QSettings under organization `GmailInboxCleaner`, separately for `Demo` and `Personal`. Operational records stay in SQLite. Provider secrets remain the external module's responsibility. Settings presents typed scalar fields in groups; list/dictionary preferences such as protections, label meanings, category overrides, model routes, and shortcuts use editable JSON. **Preview resolved dates** shows concrete local and UTC endpoints; **Save settings and replan** persists changes and recomputes pending recommendations.

| Preference group | Initial values / behavior |
| --- | --- |
| Protection | Starred mail protected; sender, domain, and label protection lists initially empty; label meanings configured by label ID. |
| Classification | Archive below 25; Keep cutoff 70; old-age archive maximum 65; uncertainty and contradictions override numeric cutoffs. |
| Execution | Dry run on; automatic execution off; automatic suggested labeling off. |
| AI | Module/model unset in live mode; default model argument omitted; optional result telemetry off; one item per prompt (1–10 allowed); local watchdog 120 seconds. |
| AI content privacy | Sender/domain/label exclusion lists; target and historical examples both respect exclusions; bodies limited to 32,000 characters initially. |
| Retrieval | HTTP multipart batches of 25 (1–50); metadata first; full eligible-body download off. |
| Execution batching | 100 messages with identical deltas per `batchModify`, configurable up to 500. |
| Retries | Maximum three retries; two-second base before exponential backoff and jitter; reliable Retry-After honored where available. |
| Context | Sent and archived context disabled; when enabled it remains read-only and subject to examination/privacy limits. |
| Learning | Enabled; explicit decisions and audit/undo remain durable when learning is paused. |
| Maintenance | 60-minute preference; periodic full inbox reconciliation every 24 hours; no OS/cloud service is installed. |
| Retention | 180 days for body-cache retention; undo/audit records are separate. |
| Logging | INFO; no bodies, raw prompts, credentials, or secret-bearing exception payloads by default. |
| Layout | Window geometry, table/filter state, and editable shortcuts persist per application mode. |
| Model routing / estimates | Exact optional route identifiers; estimated per-call cost initially zero and never presented as actual billing. |

## Learning and rules

Learning stores explicit feedback, selected examples, and scoped rules; it does not require provider fine-tuning. Inspect rule conditions, source, support, counterexamples, and state before enabling a suggestion. An approved rule remains active until separately disabled, even while learning is paused.

Bulk feedback shares a group event so thousands of selected messages do not count as thousands of independent confirmations. App-generated changes, automatic age archives, silence after automation, and unconfirmed Gmail-side changes are not confirmed training successes. Gmail history does not identify the actor or provide a complete timeline of email opens.

Pause learning to stop new example-pool additions, learned-statistics updates, rule suggestions, and historical-example sharing. Direct current-item decisions, audit, undo, and approved rules remain available. Feedback from the paused period is not retroactively included without an explicit choice.

Use Forget/reset for the intended sender/type/category scope. It removes affected examples, rebuilds derived statistics, and invalidates affected pending prediction dependencies. It does not erase action receipts or undo ownership. Rules can be exported/imported as explicit configuration; exported data may reveal sender names and should be handled as personal data.

## History, uncertainty, and undo

The ledger exists before a Gmail write and records attempts, minimal deltas, approvals, context, and receipts. A transport timeout can leave an unknown outcome: Gmail may already have committed the change. The executor inspects live state before replay rather than claiming exactly-once network execution.

Undo uses the original explicit IDs and inverse app-owned deltas. It can restore an original archived item even if the examination window has narrowed, but cannot add a new reply to the undo target list or classify its body. Later unrelated label changes are preserved. Deleted labels, intervening edits, protected state, and ambiguous ownership are conflicts for review. A preflight read cannot make Gmail atomic against simultaneous activity in another client. Gmail retains history for a limited time, so old receipts can lose ownership evidence. **Undo anyway · ownership unverified** is a separate, explicitly confirmed operation; it permits only the original inverse deltas when all current labels exactly equal the recorded after-state. It can reverse a change also desired by another client. Console users can choose `--undo ACTION_ID --undo-unverified --yes --live`; ordinary undo continues to require ownership evidence.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| OAuth test access denied | Confirm External audience, add the exact Gmail address as a test user, select Desktop app credentials, enable Gmail API. |
| Authorization expired / 401 | Reconnect. External Testing consent normally expires after seven days for this scope. |
| 403 | Inspect the classified error: missing permission or account policy is different from a quota/rate reason. Repeated retries cannot fix consent or policy. |
| 429 / transient 5xx / offline | Transient items retain a scheduled retry. The desktop resumes due waiting jobs when idle; headless runs wait and resume automatically. Explicit pauses/stops stay paused/stopped. Inspect exhausted failures in Errors. |
| Expired Gmail history | Run/resume reconciliation. Local audit and learning data are retained. |
| Invalid pagination token | Safe re-enumeration deduplicates IDs; do not clear the database to repair it. |
| Missing AI key / unsupported model | Configure the external module or exact model identifier; resume after correcting configuration. |
| Module import fails | Install its dependencies in the same venv; use a package import for relative-import dependencies; restart after environment changes. |
| Empty/invalid/partial model JSON | Inspect/retry the affected item; unvalidated or omitted targets cannot become automatic archives. |
| Pending action is stale | Refresh/review the item. New context, scope, protections, labels, or policy can invalidate the old approval. |
| Another application instance owns the directory | Use the running instance or wait for clean exit. Lock/lease recovery protects jobs; do not bypass a live owner's lock. |
| No console when diagnosing startup | Re-run with `.venv\Scripts\python.exe -m gmail_clean` so import/platform errors are visible. |


## Write limits and console opt-in

`max_actions_per_run` defaults to 1,000. A larger reviewed plan requires explicit confirmation of its exact count in **Execute this plan**. The limit also applies to automatic execution and undo; unattended execution cannot silently exceed it. Selected-action, cleanup-job, and keyboard undo paths ask for the same explicit count confirmation above the limit. Execution freezes the checked target IDs, so work added by another worker cannot expand the approved set or bypass the cap.

Headless live runs require `--live --max-actions N` or `--live --yes`. Use `--max-actions` to impose an invocation limit; `--yes` explicitly confirms execution above the configured cap. The JSON result records the execution count and plan hash. Console `--live`, model, and module overrides exist only for that invocation and never persist a live preference, including after an interrupted or crashed run.


### Disconnecting an account

**Disconnect** removes this installation's saved authorization while retaining its local database and undo history. **Disconnect and revoke** also asks Google to revoke this application's consent, after confirmation. If revocation fails, the application reports the failure; retry it or remove the application's access in your Google Account. Revocation does not erase local mail caches.

An `invalid_grant` message means Google no longer accepts the saved grant. Reconnect through browser consent. Google normally expires Gmail refresh grants after seven days for external OAuth applications whose publishing status is Testing; revocation and account changes are other possible causes. See Google's [refresh-token expiration documentation](https://developers.google.com/identity/protocols/oauth2#expiration).

### Trusting an external AI module

An external `.py` file or installed package executes Python with your user account's file and network access. Review its source and dependencies first. Saving a new or changed provider in Settings shows its resolved source, SHA-256, and a warning if other users may modify it. Approve the source only after review. The saved fingerprint is checked before AI calls, so replacing the source blocks further calls until you approve its new fingerprint. Older configurations without a fingerprint also need approval.

For headless use, `--approve-ai-module` explicitly approves the current source after you review it. Merely restarting the application does not approve changed code. The built-in offline demo is part of the application and does not need a separate source approval. Entry-file fingerprints do not cover every imported dependency or provide a sandbox; see [SECURITY.md](../SECURITY.md). The database, body cache, and backups are unencrypted at rest.


## Operational retries and AI spending

A user pause remains paused until Resume. System pauses include a reason: authentication, permission, account, and AI configuration blocks need correction; quota blocks retain their reset deadline. Temporary discovery failures resume with bounded exponential backoff. Headless execution waits in interruptible slices for at most five minutes per invocation, then retains its retry deadline for a later resume. Rate limits share one job backoff and reduce the next stage batch size without spending each item's retry attempts. Retrying errors requires a selected job or explicit work-item selection; it resets those attempts and preserves user pauses and completed work.

Settings includes AI call caps of 5,000 per job and 10,000 per UTC day by default. A cap of zero disables that individual limit. Optional estimated-cost caps require a positive configured cost per attempted call; the application pauses before a request would exceed a call or estimated-cost cap. Failed and interrupted requests remain charged against these local reservations because remote billing cannot be ruled out. Per-job caps require an explicit cap increase or a new job; a daily cap resets at the next UTC midnight. Cost figures are estimates rather than provider invoices. The job status shows reserved calls, estimated spend, and the blocking reason.

Periodic inbox reconciliation lists IDs and uses valid Gmail history to fetch metadata only for newly discovered IDs, changed IDs, and locally due policy transitions. Expired or uncovered history forces a full metadata refresh. Conversation fingerprints deliberately retain INBOX membership: the regression measurement used two sibling assessments, then observed one additional assessment when the remaining sibling was revisited after the other was archived. This preserves conservative context invalidation; archival alone does not immediately submit every sibling to AI.
