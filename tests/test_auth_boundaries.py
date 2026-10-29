"""Credential errors and native DPAPI boundaries use local fakes only."""
import ctypes
import json
from types import SimpleNamespace

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
import pytest

from gmail_clean import auth
from gmail_clean.auth import AuthenticationError, AuthManager, TokenStore


def test_restore_without_stored_credentials_does_not_open_transport(tmp_path, monkeypatch):
	manager = AuthManager(tmp_path)
	monkeypatch.setattr(manager.tokens, "load", lambda: None)
	monkeypatch.setattr(manager, "_mailbox", lambda value: pytest.fail("Missing credentials must not build a service"))
	assert manager.restore() is None
	with pytest.raises(AuthenticationError, match="Connect"):
		manager.mailbox()


def test_restore_invalid_credential_payload_is_content_free(tmp_path, monkeypatch):
	manager = AuthManager(tmp_path)
	monkeypatch.setattr(manager.tokens, "load", lambda: {"private": "must not be exposed"})
	with pytest.raises(AuthenticationError) as caught:
		manager.restore()
	assert "must not be exposed" not in str(caught.value)


def test_restore_invalid_grant_rejects_without_building_a_transport(tmp_path, monkeypatch):
	manager = AuthManager(tmp_path)
	def refresh(request):
		raise RefreshError("private provider response", {"error": "invalid_grant"})
	credentials = SimpleNamespace(valid=False, refresh=refresh)
	monkeypatch.setattr(manager.tokens, "load", lambda: {"refresh_token": "synthetic"})
	monkeypatch.setattr(Credentials, "from_authorized_user_info", lambda *args: credentials)
	with pytest.raises(AuthenticationError, match="invalid_grant") as caught:
		manager.restore()
	assert "private provider response" not in str(caught.value)


@pytest.mark.parametrize("marker", ["invalid", "keyring"])
def test_bad_backend_marker_or_corrupt_keyring_payload_is_reported(tmp_path, monkeypatch, marker):
	store = TokenStore(tmp_path)
	store.backend_path.write_text(marker)
	monkeypatch.setattr(store, "_keyring", lambda: SimpleNamespace(get_password=lambda *args: "not JSON"))
	with pytest.raises(AuthenticationError):
		store.load()


def test_selected_dpapi_payload_cannot_fall_through_to_stale_keyring(tmp_path, monkeypatch):
	store = TokenStore(tmp_path)
	store.backend_path.write_text("dpapi")
	store.path.write_bytes(b"protected data")
	monkeypatch.setattr(store, "_keyring", lambda: pytest.fail("The selected protected backend is authoritative"))
	monkeypatch.setattr(store, "_dpapi", lambda data, encrypt: json.dumps({"refresh_token": "current"}).encode())
	assert store.load() == {"refresh_token": "current"}


class NativeFunction:
	def __init__(self, callback):
		self.callback = callback

	def __call__(self, *args):
		return self.callback(*args)


def test_dpapi_native_bindings_protect_unprotect_and_free_buffers(monkeypatch):
	buffers = []
	freed = []
	def transform(encrypt):
		def call(source_pointer, *args):
			source = source_pointer._obj
			data = ctypes.string_at(source.pbData, source.cbData)
			data = b"protected:" + data if encrypt else data.removeprefix(b"protected:")
			buffer = ctypes.create_string_buffer(data)
			buffers.append(buffer)
			target = args[-1]._obj
			target.cbData = len(data)
			target.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
			return 1
		return NativeFunction(call)
	api = SimpleNamespace(crypt32=SimpleNamespace(CryptProtectData=transform(True), CryptUnprotectData=transform(False)),
		kernel32=SimpleNamespace(LocalFree=NativeFunction(lambda pointer: freed.append(pointer))))
	monkeypatch.setattr(auth, "os", SimpleNamespace(name="nt"))
	monkeypatch.setattr(ctypes, "windll", api, raising=False)
	protected = TokenStore._dpapi(b"synthetic credential", encrypt=True)
	assert protected == b"protected:synthetic credential"
	assert TokenStore._dpapi(protected, encrypt=False) == b"synthetic credential"
	assert len(freed) == 2
	api.crypt32.CryptProtectData = NativeFunction(lambda *args: 0)
	with pytest.raises(AuthenticationError, match="could not protect"):
		TokenStore._dpapi(b"synthetic credential", encrypt=True)
