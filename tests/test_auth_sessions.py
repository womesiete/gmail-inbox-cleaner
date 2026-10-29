"""A resource owner's live workers share credentials, never HTTP transports."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import threading

from google.oauth2.credentials import Credentials
import pytest

from gmail_clean.auth import AuthManager, AuthenticationError, CredentialSession, TokenStore
from gmail_clean.domain import Config
from gmail_clean.gmail import GmailError
from gmail_clean.service import AppService


ACCOUNT = 'worker@example.invalid'


def stored_payload():
	return {'refresh_token': 'synthetic-refresh', 'token_uri': 'https://oauth.example/token',
		'client_id': 'desktop', 'client_secret': 'synthetic-secret', 'scopes': ['modify']}


def source_credentials():
	payload = stored_payload()
	return Credentials(token=None, refresh_token=payload['refresh_token'], token_uri=payload['token_uri'],
		client_id=payload['client_id'], client_secret=payload['client_secret'], scopes=payload['scopes'])


def test_concurrent_workers_load_refresh_and_verify_once_but_build_distinct_transports(tmp_path, monkeypatch):
	from googleapiclient import discovery
	counts = {'load': 0, 'refresh': 0, 'profile': 0, 'save': 0}
	builds = []
	raw = source_credentials()
	def refresh(request):
		counts['refresh'] += 1
		raw.token = f'synthetic-access-{counts["refresh"]}'
		raw.expiry = datetime.now() + timedelta(hours=1)
	raw.refresh = refresh
	def load(self):
		counts['load'] += 1
		return stored_payload()
	def save(self, value):
		counts['save'] += 1
	class ProfileRequest:
		def __init__(self, credentials):
			self.credentials = credentials
		def execute(self, **kwargs):
			self.credentials.before_request(None, 'GET', 'https://gmail.example/profile', {})
			counts['profile'] += 1
			return {'emailAddress': ACCOUNT, 'historyId': '1'}
	class Service:
		def __init__(self, credentials):
			self.credentials = credentials
		def users(self):
			return self
		def getProfile(self, **kwargs):
			return ProfileRequest(self.credentials)
	def build(*args, **kwargs):
		service = Service(kwargs['credentials'])
		builds.append(service)
		return service
	monkeypatch.setattr(TokenStore, 'load', load)
	monkeypatch.setattr(TokenStore, 'save', save)
	monkeypatch.setattr(Credentials, 'from_authorized_user_info', lambda *args: raw)
	monkeypatch.setattr(discovery, 'build', build)
	(tmp_path / 'account.json').write_text(json.dumps({'account_id': ACCOUNT, 'connected': True, 'generation': 'first'}))
	service = AppService(data_dir=tmp_path, config=Config(ai_module='gmail_clean.demo_ai'))
	barrier = threading.Barrier(4)
	def worker_operation():
		barrier.wait(timeout=10)
		with service.worker() as worker:
			mailbox = worker.mailbox
			assert mailbox.account_id == ACCOUNT
			return id(mailbox.service), id(mailbox.service.credentials)
	try:
		with ThreadPoolExecutor(max_workers=4) as pool:
			results = list(pool.map(lambda _: worker_operation(), range(4)))
		assert len({transport for transport, _ in results}) == 4
		assert len({credentials for _, credentials in results}) == 1
		assert counts == {'load': 1, 'refresh': 1, 'profile': 1, 'save': 0}
		guarded = builds[0].credentials
		assert guarded.valid == raw.valid and guarded.expired == raw.expired
		assert guarded.token == raw.token and guarded.expiry == raw.expiry
		assert guarded.token_state == raw.token_state
		assert guarded.quota_project_id == raw.quota_project_id
		assert guarded.universe_domain == raw.universe_domain
		# A later automatic refresh is also serialized across transport owners.
		guarded.token = None
		guarded.expiry = datetime.now() - timedelta(seconds=1)
		with ThreadPoolExecutor(max_workers=4) as pool:
			list(pool.map(lambda box: box.credentials.before_request(None, 'GET', 'https://gmail.example/request', {}), builds))
		assert counts['refresh'] == 2 and counts['save'] == 0
		previous = builds[0].credentials
		# A fresh connection generation cannot reuse credentials from the old one.
		(tmp_path / 'account.json').write_text(json.dumps({'account_id': ACCOUNT, 'connected': True, 'generation': 'second'}))
		assert service.refresh_connection()
		with pytest.raises(GmailError, match='disconnected or changed'):
			previous.before_request(None, 'GET', 'https://gmail.example/request', {})
		with service.worker() as worker:
			assert worker.mailbox.account_id == ACCOUNT
		assert counts['load'] == 2 and counts['profile'] == 2
		with pytest.raises(GmailError, match='disconnected or changed') as caught:
			previous.before_request(None, 'GET', 'https://gmail.example/request', {})
		assert caught.value.kind == 'auth'
	finally:
		service.close()


def test_rotation_persists_once_while_other_credential_changes_do_not(tmp_path, monkeypatch):
	raw = source_credentials()
	def refresh(request):
		raw.token = 'synthetic-access'
		raw.expiry = datetime.now() + timedelta(hours=1)
		raw._refresh_token = 'synthetic-rotated'
	raw.refresh = refresh
	loads = []
	saves = []
	monkeypatch.setattr(TokenStore, 'load', lambda self: loads.append(True) or stored_payload())
	monkeypatch.setattr(TokenStore, 'save', lambda self, value: saves.append(TokenStore.payload(value)))
	monkeypatch.setattr(Credentials, 'from_authorized_user_info', lambda *args: raw)
	session = CredentialSession()
	for _ in range(3):
		manager = AuthManager(tmp_path, credential_session=session)
		monkeypatch.setattr(manager, '_mailbox', lambda value: 'verified')
		assert manager.restore() == 'verified'
		raw._scopes = ['modify', 'unchanged-token-extra-scope']
	assert len(loads) == 1 and len(saves) == 1
	assert saves[0]['refresh_token'] == 'synthetic-rotated'


def test_non_invalid_grant_refresh_error_is_transient(tmp_path, monkeypatch):
	from google.auth.exceptions import RefreshError
	raw = source_credentials()
	raw.refresh = lambda request: (_ for _ in ()).throw(RefreshError('synthetic temporary upstream failure'))
	manager = AuthManager(tmp_path)
	monkeypatch.setattr(manager.tokens, 'load', stored_payload)
	monkeypatch.setattr(Credentials, 'from_authorized_user_info', lambda *args: raw)
	with pytest.raises(GmailError) as caught:
		manager.restore()
	assert caught.value.kind == 'transient'
	assert caught.value.transient


def test_refresh_finishing_after_disconnect_cannot_restore_deleted_credentials(tmp_path, monkeypatch):
	raw = source_credentials()
	started = threading.Event()
	finish = threading.Event()
	saves = []
	def refresh(request):
		started.set()
		assert finish.wait(timeout=10)
		raw.token = 'synthetic-late-access'
		raw._refresh_token = 'synthetic-late-rotation'
	store = TokenStore(tmp_path)
	monkeypatch.setattr(store, 'save', lambda value: saves.append(value))
	monkeypatch.setattr(TokenStore, '_keyring', staticmethod(lambda: (_ for _ in ()).throw(AuthenticationError('unavailable fake'))))
	raw.refresh = refresh
	session = CredentialSession()
	guarded = session.install(raw, store, raw.refresh_token)
	manager = AuthManager(tmp_path, credential_session=session)
	with ThreadPoolExecutor(max_workers=1) as pool:
		future = pool.submit(guarded.refresh, None)
		assert started.wait(timeout=10)
		try:
			manager.disconnect()
			assert store.backend_path.read_text() == 'none'
		finally:
			finish.set()
		with pytest.raises(GmailError) as caught:
			future.result(timeout=10)
	assert caught.value.kind == 'auth'
	assert not saves
	assert store.backend_path.read_text() == 'none'
