"""Content-free operational logging and deliberately narrow diagnostic exports."""
import json
import logging
import zipfile

from gmail_clean.domain import Config
from gmail_clean.service import AppService


def test_rotating_logs_honor_level_and_export_excludes_mail_and_credentials(tmp_path):
	service = AppService(demo=True, count=2, data_dir=tmp_path, config=Config(log_level="ERROR"))
	try:
		assert service.logger.level == logging.ERROR
		service.error("classification", RuntimeError("body-secret user@example.com bearer private-token"), job_id="job-123")
		path = tmp_path / "logs" / "application.log"
		text = path.read_text(encoding="utf-8")
		assert "job-123" in text and "classification" in text
		assert "body-secret" not in text and "private-token" not in text and "user@example.com" not in text
		(tmp_path / "oauth.dpapi").write_text("credential-secret")
		archive = service.export_diagnostics(tmp_path / "diagnostics.zip")
		with zipfile.ZipFile(archive) as bundle:
			assert set(bundle.namelist()) == {"manifest.json", "logs/application.log"}
			manifest = json.loads(bundle.read("manifest.json"))
			assert manifest["schema_version"] >= 4
			assert b"credential-secret" not in b"".join(bundle.read(name) for name in bundle.namelist())
	finally:
		service.close()
