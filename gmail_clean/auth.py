"""Installed desktop OAuth and per-user protected refresh-token storage.

Only the compact refresh credential is persisted, never access tokens in QSettings.
Windows Credential Manager is preferred; DPAPI is an automatic encrypted fallback.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class AuthenticationError(RuntimeError):
	"""Authentication/configuration needs the user's intervention."""

	def __init__(self, message: str) -> None:
		super().__init__(message)
		self.safe_message = message
		self.kind = "auth"


class TokenStore:
	SERVICE = "GmailInboxCleaner"

	def __init__(self, data_dir: str | Path) -> None:
		self.data_dir = Path(data_dir)
		self.data_dir.mkdir(parents=True, exist_ok=True)
		self.path = self.data_dir / "oauth.dpapi"
		self.user = "desktop-" + hashlib.sha256(str(self.data_dir.resolve()).encode()).hexdigest()[:20]

	@staticmethod
	def _keyring() -> Any:
		import keyring
		backend = keyring.get_keyring()
		name = type(backend).__module__.lower()
		if not any(secure in name for secure in ("windows", "macos", "secretservice", "kwallet")):
			raise AuthenticationError("No supported OS-protected credential backend is available.")
		return keyring

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
		crypt = ctypes.windll.crypt32
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
			ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
			ctypes.windll.kernel32.LocalFree(target.pbData)

	def save(self, credential: Any) -> None:
		payload = {key: getattr(credential, key, None) for key in
			("refresh_token", "token_uri", "client_id", "client_secret", "scopes")}
		if not payload["refresh_token"]:
			raise AuthenticationError("Google did not return a refresh token; reconnect with consent.")
		value = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
		try:
			if len(value.encode("utf-16-le")) > 2560:
				raise AuthenticationError("Credential exceeds Windows Credential Manager's compact payload limit.")
			self._keyring().set_password(self.SERVICE, self.user, value)
			self.path.unlink(missing_ok=True)
			return
		except Exception:
			# There is deliberately no unencrypted-file fallback.
			protected = self._dpapi(value.encode(), encrypt=True)
			temporary = self.path.with_suffix(".tmp")
			with temporary.open("wb") as stream:
				stream.write(protected)
				stream.flush()
				os.fsync(stream.fileno())
			os.chmod(temporary, 0o600)
			os.replace(temporary, self.path)

	def load(self) -> dict | None:
		if self.path.exists():
			try:
				return json.loads(self._dpapi(self.path.read_bytes(), encrypt=False).decode())
			except AuthenticationError:
				raise
			except Exception as exc:
				raise AuthenticationError("Protected credentials could not be read. Reconnect this account.") from exc
		try:
			value = self._keyring().get_password(self.SERVICE, self.user)
		except Exception:
			return None
		if value is None:
			return None
		try:
			return json.loads(value)
		except (ValueError, TypeError) as exc:
			raise AuthenticationError("Stored credentials are invalid; reconnect.") from exc

	def delete(self) -> None:
		try:
			self._keyring().delete_password(self.SERVICE, self.user)
		except Exception:
			# Missing credentials or unavailable backend does not prevent DPAPI removal.
			if self.path.exists():
				self.path.unlink()
		else:
			self.path.unlink(missing_ok=True)


class AuthManager:
	def __init__(self, data_dir: str | Path, retrieval_batch_size: int = 25,
			execution_batch_size: int = 100, body_limit: int = 32_000) -> None:
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
			execution_batch_size=self.execution_batch_size, body_limit=self.body_limit)
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
		except Exception as exc:
			raise AuthenticationError("Gmail connection failed. Check the Desktop OAuth client, test-user access, consent, and network.") from exc

	def restore(self) -> Any | None:
		from google.oauth2.credentials import Credentials
		from google.auth.transport.requests import Request
		from google.auth.exceptions import TransportError
		payload = self.tokens.load()
		if not payload:
			return None
		try:
			credentials = Credentials.from_authorized_user_info(payload, SCOPES)
			if not credentials.valid:
				credentials.refresh(Request())
			box = self._mailbox(credentials)
			self.tokens.save(credentials)
			return box
		except AuthenticationError:
			raise
		except TransportError:
			from .gmail import GmailError
			raise GmailError("network", "The network is unavailable while refreshing Gmail authorization.") from None
		except Exception as exc:
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
