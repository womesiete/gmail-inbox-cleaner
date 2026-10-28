"""Protected storage and refresh failures using offline substitutes only."""
from types import SimpleNamespace

from google.auth.exceptions import TransportError
from google.oauth2.credentials import Credentials
import pytest

from gmail_clean.auth import AuthManager, AuthenticationError, TokenStore
from gmail_clean.gmail import GmailError


def credential():
	return SimpleNamespace(refresh_token="synthetic-refresh", token_uri="https://oauth2.googleapis.com/token",
		client_id="synthetic-client", client_secret="synthetic-secret", scopes=["scope"])


def test_keyring_fake_roundtrips_without_creating_fallback_file(tmp_path, monkeypatch):
	values = {}
	backend = SimpleNamespace(set_password=lambda service, user, value: values.update({(service, user): value}),
		get_password=lambda service, user: values.get((service, user)), delete_password=lambda service, user: values.pop((service, user), None))
	monkeypatch.setattr(TokenStore, "_keyring", staticmethod(lambda: backend))
	store = TokenStore(tmp_path)
	store.save(credential())
	assert store.load()["refresh_token"] == "synthetic-refresh"
	assert not store.path.exists()
	store.delete()
	assert store.load() is None


def test_dpapi_fake_roundtrips_and_corruption_is_reported(tmp_path, monkeypatch):
	def unavailable():
		raise AuthenticationError("keyring locked")
	def protected(data, encrypt):
		if encrypt:
			return b"protected:" + data
		if not data.startswith(b"protected:"):
			raise AuthenticationError("invalid protected bytes")
		return data.removeprefix(b"protected:")
	monkeypatch.setattr(TokenStore, "_keyring", staticmethod(unavailable))
	monkeypatch.setattr(TokenStore, "_dpapi", staticmethod(protected))
	store = TokenStore(tmp_path)
	store.save(credential())
	assert store.load()["client_id"] == "synthetic-client"
	store.path.write_bytes(b"corrupted")
	with pytest.raises(AuthenticationError):
		store.load()


def test_refresh_transport_failure_is_network_and_does_not_discard_credentials(tmp_path, monkeypatch):
	manager = AuthManager(tmp_path)
	manager.tokens.load = lambda: {"refresh_token": "synthetic"}
	class RefreshFailure:
		valid = False
		def refresh(self, request):
			raise TransportError("offline transport failure")
	monkeypatch.setattr(Credentials, "from_authorized_user_info", lambda *args, **kwargs: RefreshFailure())
	with pytest.raises(GmailError) as captured:
		manager.restore()
	assert captured.value.kind == "network"
	assert manager.tokens.load() == {"refresh_token": "synthetic"}
