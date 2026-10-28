# Windows and account setup

## Python, virtual environment, and PyCharm

Use Windows 10/11 and Python 3.11. A regular project virtual environment is supported; Conda and system-wide installation are unnecessary.

```powershell
cd C:\Projects\gmail-inbox-cleaner
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m gmail_clean --demo
```

In PyCharm, open the repository and select `.venv\Scripts\python.exe` as the existing interpreter. Add a Python run configuration with module name `gmail_clean`, project directory as the working directory, and parameters `--demo`. Remove `--demo` for the real account. Install your private module and its dependencies into this same environment.

For a console-free launch:

```powershell
.\.venv\Scripts\pythonw.exe -m gmail_clean --demo
.\.venv\Scripts\gmail-clean-gui.exe --demo
```

A shortcut can target the absolute path of `pythonw.exe`, with `-m gmail_clean` as its arguments and the project directory as its working directory. `scripts\launch_windows.pyw` locates the project's `.venv\Scripts\pythonw.exe` when present. Test initially with `python.exe` so startup errors are visible.

## Personal Gmail OAuth

Integration instructions were checked against official Google documentation on **2026-10-28**. Console wording and account restrictions can change; follow the linked Google pages if a screen differs.

1. Create/select a Google Cloud project and enable the **Gmail API**.
2. Configure the Google Auth platform Branding and Audience screens. A personal `@gmail.com` account uses an **External** audience. The Workspace-only Internal audience shown in some quickstart examples does not apply to this personal-account setup.
3. Configure Data Access for `https://www.googleapis.com/auth/gmail.modify`. This application uses that scope for reading and message label changes. It does not request the broader `https://mail.google.com/` scope.
4. In Testing status, add your own Gmail address as a test user. Create an OAuth client with application type **Desktop app**. Download its client JSON outside the repository.
5. Launch the real application, use its account connection control, and select that JSON file. The Google client library opens browser consent with a local callback. Select the intended Gmail account, grant the requested scope, and verify the connected account shown in the application before creating a live job.

Google's [Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python) describes API enablement and Desktop credentials. [OAuth app states](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview) distinguish personal External setup from organization-only Internal setup.

### Consent, token expiration, and verification

For External apps in **Testing**, Gmail consent and offline refresh tokens expire after seven days; the basic identity-only exception does not cover `gmail.modify`. Reconnect when consent expires. Revocation and other token invalidation can require reconnecting at other times too. See [Manage app audience](https://support.google.com/cloud/answer/15549945) and [refresh-token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).

`gmail.modify` is a restricted scope. Google's [personal-use exception](https://support.google.com/cloud/answer/13464323) can permit a small personal app without full verification, with unverified-app warnings and limits. Publishing more broadly, processing restricted data through servers, or changing the use case can introduce verification/security-assessment obligations. This project does not promise unrestricted or universally verification-free deployment. Google API data-use policies still apply, including when configured AI routing sends email content to another service. Consult [scope requirements](https://developers.google.com/workspace/gmail/api/auth/scopes) and [restricted-scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification).

### Disconnect and reconnect

Disconnect removes the locally stored authorization and prevents further calls from that local connection. For server-side revocation, remove the application's access in your Google Account's third-party connections. Reconnect through browser consent after expired/revoked credentials. A different Gmail identity cannot reuse an existing account-bound operational database; select a separate data directory if intentionally changing accounts.

## Token and local-data protection

Refresh credentials are kept out of QSettings, SQLite, source control, and normal logs. On Windows, compact credential JSON is stored through `keyring` in Windows Credential Manager, with DPAPI per-user encryption as a fallback. No plaintext-file fallback is provided. The OAuth client JSON and private module remain outside source control.

Message caches, SQLite databases, exported rules, QSettings, and backups are **not inherently encrypted**. They rely on the Windows user's directory permissions and device protection. Use a private Windows account, appropriate folder ACLs, and disk encryption if needed. Check permissions when choosing `--data-dir` or a backup destination. A shared or synchronized folder is a deliberate exposure choice. Retention settings can remove cached bodies; removing cache content does not remove Gmail messages or audit receipts.

## External AI module

Choose a normal importable package name or an absolute path to a standalone `.py` file in Settings. Package import is preferable when the module relies on relative imports. Standalone loading does not install dependencies or invent a package context.

The minimum module interface is:

```python
def call_api(prompt: str, model: str = DEFAULT_MODEL) -> str:
	"""Return response text, or raise a provider/configuration exception."""
```

The module owns its credentials, provider routing, SDK configuration, and response extraction. Configure required environment variables before launching/importing it. Install dependencies using `.venv\Scripts\python.exe -m pip install ...`; the application does not replace the module with its own provider SDK.

Leave the **model** field empty to use the module's default: the adapter calls `call_api(prompt)` and omits `model`. Enter an exact supported identifier to call `call_api(prompt, model=identifier)`. No aliases or model-discovery API are assumed. Do not type the literal `DEFAULT_MODEL` unless that is an actual identifier your module supports.

Optional `call_api_result` telemetry is disabled initially. Enable it only when the module exports that function and returns a result with `text`, `model`, `provider`, and `duration_s`. It uses the same model-argument omission rule; ordinary `call_api` remains sufficient.

The module is loaded once inside a dedicated spawned worker process. Calls are serial and contain their own context. Changing module settings restarts the worker at a safe boundary. SQLite connections stay in the main application process. Named exported exception classes are matched by class identity. Missing/unsupported credentials or models pause AI work; rate limits and invalid responses have bounded retry handling. Unknown provider exceptions remain visible for inspection.

Pause stops new calls and allows the current call to finish. A watchdog can stop the **local child process**, but cannot promise cancellation of a remote request; it may finish or be billed. Usage/cost without provider telemetry is an estimate, not a measured token bill. The app neither executes instructions in email nor grants the AI mailbox tools.

## Optional Windows distribution

Run on Windows in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package_windows.ps1
# Include packages imported only by a private AI module when building:
powershell -ExecutionPolicy Bypass -File scripts\package_windows.ps1 -HiddenImports my_ai_package
```

This creates `dist\GmailInboxCleaner\GmailInboxCleaner.exe` with PyInstaller's windowed, one-directory mode. Keep the directory together. Credentials, mailbox caches, and private `.py` files are not bundled. Arbitrary external-module dependencies may need extra PyInstaller hooks or hidden imports. The source virtual-environment route remains the supported development setup; smoke-test the packaged application and spawned AI worker on the target Windows PC.
