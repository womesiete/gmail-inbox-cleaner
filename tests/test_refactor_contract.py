"""Characterize the supported desktop facade and durable stage-stop behavior."""
from datetime import UTC, datetime

from PyQt6.QtCore import QSettings, QUrl

from gmail_clean.domain import Config, resolve_window
from gmail_clean.gui import SafePreview, SettingsEditor, TaskSignals, format_window, load_preferences, save_preferences
from gmail_clean.service import AppService


def test_desktop_public_facade_keeps_preferences_preview_and_signal_contract(qapp, tmp_path):
	settings = QSettings(str(tmp_path / 'facade.ini'), QSettings.Format.IniFormat)
	config = Config(timezone='America/New_York', extras={'future_option': 'preserved'})
	save_preferences(config, settings)
	loaded = load_preferences(settings)
	assert loaded.extras['future_option'] == 'preserved'
	editor = SettingsEditor(loaded)
	assert editor.read_config().timezone == 'America/New_York'
	caption = format_window(resolve_window(loaded, datetime(2026, 10, 28, 12, tzinfo=UTC)))
	assert 'America/New_York' in caption and 'UTC:' in caption
	preview = SafePreview()
	preview.set_message('<b>mail text</b>')
	assert preview.toPlainText() == '<b>mail text</b>'
	assert preview.loadResource(2, QUrl('https://example.invalid/pixel')).isEmpty()
	signals = TaskSignals()
	seen = []
	signals.progress.connect(lambda task, stage: seen.append((task, stage)))
	signals.progress.emit('task', 'metadata')
	assert seen == [('task', 'metadata')]
	preview.close()
	editor.close()


def test_stage_stop_preserves_frozen_scope_and_unresolved_items(tmp_path):
	service = AppService(data_dir=tmp_path, demo=True, count=4,
		clock=lambda: datetime(2026, 10, 28, 12, tzinfo=UTC), config=Config(timezone='UTC'))
	try:
		job = service.new_job()
		service.jobs.queue(job, 'already-discovered', 'metadata')
		service.jobs.save_payload(job, {'discovered': True})
		anchor = service.store.one('SELECT anchor_utc FROM jobs WHERE id=?', (job,))['anchor_utc']
		result = service.run_job(job, should_stop=lambda: 'paused')
		assert result['state'] == 'paused'
		assert service.store.one('SELECT state FROM work_items WHERE job_id=?', (job,))['state'] == 'pending'
		assert service.store.one('SELECT anchor_utc FROM jobs WHERE id=?', (job,))['anchor_utc'] == anchor
		assert service.mailbox.mutations == []
	finally:
		service.close()
