"""Installed desktop OAuth and per-user protected refresh-token storage.

Only the compact refresh credential is persisted, never access tokens in QSettings.
Windows Credential Manager is preferred; DPAPI is an automatic encrypted fallback.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import uuid
import warnings
from functools import wraps
from pathlib import Path
from typing import Any

from .runtime import path_lock

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _serialized_credentials(method):
	"""Keep every protected-file reader inside the atomic replacement protocol."""
	@wraps(method)
	def call(self, *args, **kwargs):
		with path_lock(self.path):
			return method(self, *args, **kwargs)
	return call


class AuthenticationError(RuntimeError):
	"""Authentication/configuration needs the user's intervention."""

	def __init__(self, message: str, *, backend_name: str = "") -> None:
		if backend_name:
			message += f" Resolved credential backend: {backend_name}."
		super().__init__(message)
		self.safe_message = message
		self.kind = "auth"
		self.backend_name = backend_name


class TokenStore:
	SERVICE = "GmailInboxCleaner"

	def __init__(self, data_dir: str | Path) -> None:
		self.data_dir = Path(data_dir)
		self.data_dir.mkdir(parents=True, exist_ok=True)
		self.path = self.data_dir / "oauth.dpapi"
		self.backend_path = self.data_dir / "oauth.backend"
		self.user = "desktop-" + hashlib.sha256(str(self.data_dir.resolve()).encode()).hexdigest()[:20]

	@staticmethod
	def _keyring() -> Any:
		import keyring
		backend = keyring.get_keyring()
		candidates = []
		visited = set()
		def resolve(candidate):
			if id(candidate) in visited:
				return
			visited.add(id(candidate))
			name = type(candidate).__module__.lower()
			if "chainer" in name or type(candidate).__name__ == "ChainerBackend":
				for child in candidate.backends:
					resolve(child)
				return
			if any(secure in name for secure in ("windows", "macos", "secretservice", "libsecret", "kwallet")):
				try:
					priority = float(candidate.priority)
				except Exception:  # noqa: BLE001 - unavailable keyring backends can raise while probing priority.
					return
				if priority > 0:
					candidates.append((priority, candidate))
		resolve(backend)
		if not candidates:
			raise AuthenticationError("No supported OS-protected credential backend is available.",
				backend_name=TokenStore._backend_name(backend))
		# Use the selected backend directly: the chainer can otherwise route to a
		# disallowed member even when it contains a secure candidate.
		return max(candidates, key=lambda candidate: candidate[0])[1]

	@staticmethod
	def _backend_name(backend: Any) -> str:
		return f"{type(backend).__module__}.{type(backend).__name__}"

	@staticmethod
	def backend_name() -> str:
		"""Describe the resolved protected backend without accessing credentials."""
		try:
			return TokenStore._backend_name(TokenStore._keyring())
		except AuthenticationError as exc:
			return f"Unavailable ({exc.backend_name or 'protected keyring'})"
		except Exception as exc:  # noqa: BLE001 - preferences must survive absent or locked OS integration.
			return f"Unavailable ({type(exc).__name__})"

	@staticmethod
	def _dpapi(data: bytes, encrypt: bool) -> bytes:
		if os.name != "nt":
			raise AuthenticationError("Secure token storage is unavailable. Install an OS keyring backend.")
		from ctypes import wintypes

		class DataBlob(ctypes.Structure):
			_fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

		buffer = ctypes.create_string_buffer(data)
		source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
		target = DataBlob()
		windows_api = getattr(ctypes, "windll")  # noqa: B009 - platform API is absent from other-platform type stubs and loaded lazily
		crypt = windows_api.crypt32
		fn = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
		fn.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
			ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
		fn.restype = wintypes.BOOL
		# CRYPTPROTECT_UI_FORBIDDEN; user scope is the default (no LOCAL_MACHINE flag).
		if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
			raise AuthenticationError("Windows could not protect/unprotect this user's credential.")
		try:
			return ctypes.string_at(target.pbData, target.cbData)
		finally:
			windows_api.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
			windows_api.kernel32.LocalFree(target.pbData)

	@staticmethod
	def payload(credential: Any) -> dict:
		return {key: getattr(credential, key, None) for key in
			("refresh_token", "token_uri", "client_id", "client_secret", "scopes")}

	def _select_backend(self, name: str) -> None:
		"""Persist only the selected backend name; this file contains no secrets."""
		temporary = self.backend_path.with_name(f"{self.backend_path.name}.{uuid.uuid4().hex}.tmp")
		try:
			with temporary.open("x", encoding="ascii") as stream:
				stream.write(name)
				stream.flush()
				os.fsync(stream.fileno())
			os.chmod(temporary, 0o600)
			os.replace(temporary, self.backend_path)
		finally:
			temporary.unlink(missing_ok=True)

	@_serialized_credentials
	def save(self, credential: Any) -> None:
		payload = self.payload(credential)
		if not payload["refresh_token"]:
			raise AuthenticationError("Google did not return a refresh token; reconnect with consent.")
		value = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
		backend = None
		try:
			backend = self._keyring()
			if os.name == "nt" and len(value.encode("utf-16-le")) > 2560:
				raise AuthenticationError("Credential exceeds Windows Credential Manager's compact payload limit.")
			backend.set_password(self.SERVICE, self.user, value)
		except Exception as keyring_error:  # noqa: BLE001 - OS keyring backends expose different exception types.
			# There is deliberately no unencrypted-file fallback.
			try:
				protected = self._dpapi(value.encode(), encrypt=True)
			except Exception:  # noqa: BLE001 - Preserve the original keyring failure when encrypted fallback fails.
				raise AuthenticationError("The OS credential backend could not save authorization. Unlock or configure the protected keyring and reconnect.",
					backend_name=self._backend_name(backend) if backend is not None else getattr(keyring_error, "backend_name", "unavailable")) from keyring_error
			temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
			try:
				with temporary.open("xb") as stream:
					stream.write(protected)
					stream.flush()
					os.fsync(stream.fileno())
				os.chmod(temporary, 0o600)
				os.replace(temporary, self.path)
			finally:
				temporary.unlink(missing_ok=True)
			try:
				self._select_backend("dpapi")
			except OSError as exc:
				raise AuthenticationError("Authorization was protected, but its backend marker could not be updated. Check local file access and reconnect.", backend_name="Windows DPAPI") from exc
		else:
			try:
				self._select_backend("keyring")
			except OSError as exc:
				raise AuthenticationError("Authorization was protected, but its backend marker could not be updated. Check local file access and reconnect.", backend_name=self._backend_name(backend)) from exc
			self.path.unlink(missing_ok=True)

	@_serialized_credentials
	def load(self) -> dict | None:
		try:
			backend = self.backend_path.read_text(encoding="ascii").strip()
		except FileNotFoundError:
			backend = ""
		if backend not in {"", "keyring", "dpapi", "none"}:
			raise AuthenticationError("The protected credential backend selection is invalid; reconnect.")
		if backend == "none":
			return None
		# The marker is authoritative after a save. Legacy stores without a marker
		# prefer the OS keyring so an abandoned DPAPI file cannot hide current data.
		value = None
		keyring_error = None
		if backend != "dpapi":
			try:
				value = self._keyring().get_password(self.SERVICE, self.user)
			except Exception as exc:  # noqa: BLE001 - Third-party keyring failures stay behind the protected-store boundary.
				keyring_error = exc
			if value is not None:
				try:
					return json.loads(value)
				except (ValueError, TypeError) as exc:
					raise AuthenticationError("Stored credentials are invalid; reconnect.") from exc
		if backend == "dpapi" or (not backend and self.path.exists()):
			try:
				return json.loads(self._dpapi(self.path.read_bytes(), encrypt=False).decode())
			except AuthenticationError:
				raise
			except (OSError, UnicodeError, ValueError) as exc:
				raise AuthenticationError("Protected credentials could not be read. Reconnect this account.") from exc
		if backend == "keyring" and keyring_error is not None:
			raise AuthenticationError("The OS credential backend could not read authorization. Unlock the protected keyring and retry.", backend_name=self.backend_name()) from keyring_error
		return None

	@_serialized_credentials
	def delete(self) -> None:
		try:
			self._keyring().delete_password(self.SERVICE, self.user)
		except Exception:  # noqa: BLE001 - Backend failures must still remove the encrypted-file copy.
			# Missing credentials or unavailable backend does not prevent DPAPI removal.
			if self.path.exists():
				self.path.unlink()
		else:
			self.path.unlink(missing_ok=True)
		# An unavailable keyring may still contain an obsolete credential. Keep a
		# non-secret tombstone so reconnect cannot silently revive that copy.
		self._select_backend("none")


class AuthManager:
	def __init__(self, data_dir: str | Path, retrieval_batch_size: int = 25,
			execution_batch_size: int = 100, body_limit: int = 32_000, quota_limiter=None) -> None:
		self.quota_limiter = quota_limiter
		self.tokens = TokenStore(data_dir)
		self.retrieval_batch_size = retrieval_batch_size
		self.execution_batch_size = execution_batch_size
		self.body_limit = body_limit
		self.credentials: Any = None

	def _mailbox(self, credentials: Any) -> Any:
		from googleapiclient.discovery import build
		from .gmail import GmailMailbox
		service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
		box = GmailMailbox(service, retrieval_batch_size=self.retrieval_batch_size,
			execution_batch_size=self.execution_batch_size, body_limit=self.body_limit, quota_limiter=self.quota_limiter)
		box.identity()  # Verify the actually connected account, never trust a UI hint.
		self.credentials = credentials
		return box

	def connect(self, client_secrets_path: str | Path) -> Any:
		from google_auth_oauthlib.flow import InstalledAppFlow
		path = Path(client_secrets_path).expanduser().resolve()
		if not path.is_file():
			raise AuthenticationError("Choose the downloaded Desktop app OAuth client JSON file.")
		try:
			settings = json.loads(path.read_text(encoding="utf-8"))
			if "installed" not in settings:
				raise AuthenticationError("Use an OAuth Desktop app client, not a Web/service-account client.")
			flow = InstalledAppFlow.from_client_config(settings, SCOPES)
			credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent", timeout_seconds=180,
				authorization_prompt_message="Open the browser to authorize Gmail Inbox Cleaner.",
				success_message="Authorization complete. You may close this window.")
			box = self._mailbox(credentials)
			self.tokens.save(credentials)
			return box
		except AuthenticationError:
			raise
		except Exception as exc:  # noqa: BLE001 - OAuth/provider errors need a content-free user-facing summary.
			raise AuthenticationError("Gmail connection failed. Check the Desktop OAuth client, test-user access, consent, and network.") from exc

	def restore(self) -> Any | None:
		from google.oauth2.credentials import Credentials
		from google.auth.transport.requests import Request
		from google.auth.exceptions import RefreshError, TransportError
		payload = self.tokens.load()
		if not payload:
			return None
		try:
			credentials = Credentials.from_authorized_user_info(payload, SCOPES)
			if not credentials.valid:
				credentials.refresh(Request())
			box = self._mailbox(credentials)
			if self.tokens.payload(credentials) != payload:
				try:
					self.tokens.save(credentials)
				except Exception:  # noqa: BLE001 - A credential persistence failure cannot invalidate the refreshed in-memory session.
					warnings.warn("Gmail authorization refreshed, but changed credentials could not be saved. Unlock the credential backend or reconnect before restarting.", RuntimeWarning, stacklevel=2)
			return box
		except AuthenticationError:
			raise
		except TransportError:
			from .gmail import GmailError
			raise GmailError("network", "The network is unavailable while refreshing Gmail authorization.") from None
		except RefreshError as exc:
			if any((isinstance(value, dict) and value.get("error") == "invalid_grant")
				or (isinstance(value, str) and "invalid_grant" in value) for value in exc.args):
				raise AuthenticationError("Google rejected authorization (invalid_grant). Reconnect this account. External OAuth apps in Testing normally issue Gmail grants that expire after 7 days; revocation or account changes can also invalidate them.") from exc
			raise AuthenticationError("Gmail authorization could not be refreshed. Reconnect and check your OAuth app configuration.") from exc
		except Exception as exc:  # noqa: BLE001 - Unexpected credential errors cannot expose token/response details.
			raise AuthenticationError("Gmail authorization could not be refreshed. Reconnect; test-mode grants may expire.") from exc

	def mailbox(self) -> Any:
		box = self.restore()
		if box is None:
			raise AuthenticationError("Connect a Gmail account before starting a live job.")
		return box

	def disconnect(self, revoke: bool = False) -> None:
		"""Remove local credentials; optional revocation also removes Google consent."""
		if revoke:
			payload = self.tokens.load()
			if payload and payload.get("refresh_token"):
				import requests
				try:
					response = requests.post("https://oauth2.googleapis.com/revoke",
						data={"token": payload["refresh_token"]}, timeout=15)
					if response.status_code not in {200, 400}:
						raise AuthenticationError("Google could not revoke consent. Retry or remove access in your Google Account.")
				except requests.RequestException as exc:
					raise AuthenticationError("Consent revocation failed because of a network error.") from exc
		self.tokens.delete()
		self.credentials = None
