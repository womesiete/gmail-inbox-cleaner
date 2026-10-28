# Security and privacy

## Trust boundaries

Gmail Inbox Cleaner runs locally with your operating-system account's permissions. Gmail messages, HTML, and AI responses are untrusted input. Mail content cannot directly select arbitrary Gmail actions: validated assessments pass through the policy, review ledger, execution preflight, and ownership-aware undo checks. These checks do not make separate Gmail requests atomic.

An external AI provider is executable Python code, **not a sandboxed plugin**. Its `call_api` implementation and imported dependencies can read files, access credentials available to your user, make network calls, and send content to providers. The separate process supports local cancellation and lifecycle management; it is not a security boundary against malicious code. Review the module and dependencies before selecting it. A filename or package name is not proof that it is safe.

When configuration is approved, the application records a SHA-256 of the entry source. Settings show the resolved source, fingerprint, and write-permission warning before approval. Changed entry source blocks new application AI calls until explicitly approved again. Existing configurations without a fingerprint need approval. CLI `--approve-ai-module` explicitly approves the current source after your manual review. A direct `AIClient` constructor is that library caller's explicit configuration boundary and records its initial source fingerprint.

On POSIX systems, inspection warns when the source or a containing directory has group/other write bits. On Windows, the app explicitly reports that ACL permissions were not verified; store provider code in a folder that other users cannot modify. Fingerprints cover the entry source only. They do not authenticate authors, audit dependencies, track every imported package, prevent another local process from changing configuration, or defend against an attacker with your account's permissions. Standalone `.py` loading compiles the exact approved source bytes rather than trusting a bytecode cache. Installed packages still use Python's import machinery and depend on the integrity of the installed environment.

## Credentials and data at rest

Refresh credentials use a supported OS keyring, with per-user Windows DPAPI as an encrypted fallback. There is no plaintext credential fallback. A non-secret `oauth.backend` marker selects the authoritative backend, including a disconnected tombstone; an abandoned copy in another backend is never silently preferred. Unlock a failed backend or reconnect if updated credentials cannot be saved. An in-memory refreshed session remains usable when saving a changed compact credential fails, but restarting may require reconnecting.

**SQLite databases, message bodies, snippets, metadata, learning examples, backups, diagnostic exports, and local caches are not encrypted by this application.** File permissions are tightened where supported, but that does not protect against another process running as the same user, an administrator, stolen unlocked devices, or a compromised OS. Use OS full-disk encryption and account controls appropriate to your data. Protect or remove backups and exports as carefully as the primary data. Cache pruning and SQLite secure deletion do not guarantee forensic erasure from SSDs, filesystem snapshots, cloud backups, or previously copied files.

The external provider receives the selected prompt content allowed by current exclusions, window, and context settings. Provider retention, cost, credentials, and privacy policy belong to that provider/module. Never put secrets in ordinary settings. Logs and diagnostics must stay content-free and sanitized; review an export before sharing it.

## Disconnecting and reporting

Disconnect removes this installation's saved authorization; Disconnect and revoke also requests removal of Google consent after confirmation. Neither erases the local database or backup copies. Revocation failure is reported so it can be retried or handled in Google Account settings.

Report a suspected vulnerability privately to the repository owner through GitHub's private vulnerability reporting feature when available. Include a minimal synthetic reproduction and versions. Do not post real messages, access/refresh tokens, client secrets, body caches, or unredacted database files in public issues. No security response service-level agreement is promised by this personal project.
