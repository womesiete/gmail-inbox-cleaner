"""Secure backend selection uses fakes rather than a desktop keyring session."""
from types import SimpleNamespace

import pytest

from gmail_clean.auth import AuthenticationError, TokenStore


class MemoryBackend:
	priority = 1

	def __init__(self):
		self.value = None

	def set_password(self, service, user, value):
		self.value = value

	def get_password(self, service, user):
		return self.value


def backend(module, priority):
	return type('SecureBackend', (MemoryBackend,), {'__module__': module, 'priority': priority})()


def credential(token='synthetic-refresh'):  # noqa: S107 - protected-storage fixture cannot authorize an account.
	return SimpleNamespace(refresh_token=token, token_uri='https://oauth.example/token',
		client_id='desktop', client_secret='synthetic', scopes=['modify'])


def test_chainer_selects_highest_priority_secure_backend_and_never_routes_to_plaintext(monkeypatch, tmp_path):
	import keyring
	secret_service = backend('keyring.backends.SecretService', 5)
	libsecret = backend('keyring.backends.libsecret', 6)
	plaintext = backend('keyrings.alt.file', 99)
	chain = type('ChainerBackend', (), {'__module__': 'keyring.backends.chainer',
		'backends': [plaintext, secret_service, libsecret]})()
	monkeypatch.setattr(keyring, 'get_keyring', lambda: chain)
	store = TokenStore(tmp_path)
	assert store._keyring() is libsecret
	assert store.backend_name() == 'keyring.backends.libsecret.SecureBackend'
	store.save(credential())
	assert store.load()['refresh_token'] == 'synthetic-refresh'
	assert plaintext.value is None and secret_service.value is None


def test_chainer_with_no_secure_members_reports_resolved_backend(monkeypatch):
	import keyring
	chain = type('ChainerBackend', (), {'__module__': 'keyring.backends.chainer',
		'backends': [backend('keyrings.alt.file', 99)]})()
	monkeypatch.setattr(keyring, 'get_keyring', lambda: chain)
	with pytest.raises(AuthenticationError, match='keyring.backends.chainer.ChainerBackend'):
		TokenStore._keyring()


def test_non_windows_keyring_accepts_payload_larger_than_windows_limit(monkeypatch, tmp_path):
	import gmail_clean.auth as auth
	memory = MemoryBackend()
	monkeypatch.setattr(TokenStore, '_keyring', staticmethod(lambda: memory))
	monkeypatch.setattr(auth, 'os', SimpleNamespace(**{**vars(auth.os), 'name': 'posix'}))
	store = TokenStore(tmp_path)
	store.save(credential('synthetic-' * 1000))
	assert len(memory.value.encode('utf-16-le')) > 2560
	assert store.load()['refresh_token'] == 'synthetic-' * 1000


def test_successful_keyring_write_with_marker_failure_is_authentication_error(monkeypatch, tmp_path):
	memory = MemoryBackend()
	store = TokenStore(tmp_path)
	monkeypatch.setattr(TokenStore, '_keyring', staticmethod(lambda: memory))
	failure = OSError('synthetic marker access denied')
	monkeypatch.setattr(store, '_select_backend', lambda name: (_ for _ in ()).throw(failure))
	with pytest.raises(AuthenticationError, match='backend marker') as caught:
		store.save(credential())
	assert memory.value is not None
	assert caught.value.__cause__ is failure
	assert 'MemoryBackend' in str(caught.value)


def test_personal_settings_shows_resolved_backend(qtbot, monkeypatch):
	from gmail_clean.domain import Config
	from gmail_clean.gui_preferences import SettingsEditor
	monkeypatch.setattr(TokenStore, 'backend_name', staticmethod(lambda: 'keyring.backends.libsecret.SecureBackend'))
	editor = SettingsEditor(Config())
	qtbot.addWidget(editor)
	assert 'keyring.backends.libsecret.SecureBackend' in editor.credential_backend.text()
