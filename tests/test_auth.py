"""Protected credential persistence and refresh boundaries use offline fakes."""

from types import SimpleNamespace

import pytest

from gmail_clean.auth import AuthManager, AuthenticationError, TokenStore


def credential(token="refresh"):
	return SimpleNamespace(refresh_token=token, token_uri="https://oauth.example/token", client_id="desktop", client_secret="secret", scopes=["modify"], valid=False)


class FakeKeyring:
	def __init__(self):
		self.value = None

	def set_password(self, service, user, value):
		self.value = value

	def get_password(self, service, user):
		return self.value

	def delete_password(self, service, user):
		self.value = None


def test_restore_does_not_resave_unchanged_refresh_payload(tmp_path, monkeypatch):
	from google.oauth2.credentials import Credentials
	manager = AuthManager(tmp_path)
	current = credential()
	current.refresh = lambda request: None
	payload = {key: getattr(current, key) for key in ("refresh_token", "token_uri", "client_id", "client_secret", "scopes")}
	monkeypatch.setattr(manager.tokens, "load", lambda: payload)
	monkeypatch.setattr(Credentials, "from_authorized_user_info", lambda payload, scopes: current)
	monkeypatch.setattr(manager, "_mailbox", lambda value: "verified-mailbox")
	monkeypatch.setattr(manager.tokens, "save", lambda value: pytest.fail("Unchanged compact credentials must not be saved"))
	assert manager.restore() == "verified-mailbox"


def test_valid_refresh_survives_failure_to_save_rotated_token(tmp_path, monkeypatch):
	from google.oauth2.credentials import Credentials
	manager = AuthManager(tmp_path)
	current = credential("rotated")
	current.refresh = lambda request: None
	payload = {key: getattr(credential(), key) for key in ("refresh_token", "token_uri", "client_id", "client_secret", "scopes")}
	monkeypatch.setattr(manager.tokens, "load", lambda: payload)
	monkeypatch.setattr(Credentials, "from_authorized_user_info", lambda payload, scopes: current)
	monkeypatch.setattr(manager, "_mailbox", lambda value: "verified-mailbox")
	monkeypatch.setattr(manager.tokens, "save", lambda value: (_ for _ in ()).throw(RuntimeError("locked keyring with private details")))
	with pytest.warns(RuntimeWarning, match="refreshed.*could not be saved"):
		assert manager.restore() == "verified-mailbox"


def test_keyring_save_failure_retains_original_cause_when_fallback_fails(tmp_path, monkeypatch):
	store = TokenStore(tmp_path)
	locked = RuntimeError("synthetic locked keyring")
	monkeypatch.setattr(store, "_keyring", lambda: (_ for _ in ()).throw(locked))
	monkeypatch.setattr(store, "_dpapi", lambda data, encrypt: (_ for _ in ()).throw(AuthenticationError("unavailable fallback")))
	with pytest.raises(AuthenticationError) as caught:
		store.save(credential())
	assert caught.value.__cause__ is locked
	assert "credential backend" in str(caught.value)


def test_backend_selection_is_authoritative_and_legacy_prefers_keyring(tmp_path, monkeypatch):
	import json
	store = TokenStore(tmp_path)
	backend = FakeKeyring()
	monkeypatch.setattr(store, "_keyring", lambda: backend)
	monkeypatch.setattr(store, "_dpapi", lambda data, encrypt: b"encrypted:" + data if encrypt else data.removeprefix(b"encrypted:"))
	store.path.write_bytes(b'encrypted:{"refresh_token":"stale-dpapi"}')
	backend.value = json.dumps({"refresh_token": "current-keyring"})
	assert store.load()["refresh_token"] == "current-keyring"
	store.save(credential("new-keyring"))
	assert not store.path.exists()
	original = backend.set_password
	backend.set_password = lambda *args: (_ for _ in ()).throw(RuntimeError("keyring locked"))
	store.save(credential("new-dpapi"))
	backend.set_password = original
	assert store.load()["refresh_token"] == "new-dpapi"


def test_disconnect_tombstone_does_not_restore_stale_unavailable_backend(tmp_path, monkeypatch):
	store = TokenStore(tmp_path)
	backend = FakeKeyring()
	monkeypatch.setattr(store, "_keyring", lambda: backend)
	store.save(credential())
	backend.delete_password = lambda *args: (_ for _ in ()).throw(RuntimeError("backend locked"))
	store.delete()
	assert backend.value is not None
	assert store.load() is None


def test_invalid_grant_explains_testing_expiry_without_exposing_response(tmp_path, monkeypatch):
	from google.auth.exceptions import RefreshError
	from google.oauth2.credentials import Credentials
	manager = AuthManager(tmp_path)
	current = credential()
	current.refresh = lambda request: (_ for _ in ()).throw(RefreshError("invalid_grant: private-response", {"error": "invalid_grant"}))
	monkeypatch.setattr(manager.tokens, "load", lambda: {"refresh_token": "stored"})
	monkeypatch.setattr(Credentials, "from_authorized_user_info", lambda payload, scopes: current)
	with pytest.raises(AuthenticationError) as caught:
		manager.restore()
	assert "invalid_grant" in str(caught.value)
	assert "7 days" in str(caught.value)
	assert "private-response" not in str(caught.value)
