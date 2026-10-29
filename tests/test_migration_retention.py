"""Keep recovery snapshots bounded without discarding a failed upgrade's evidence."""
import os
from pathlib import Path
import sqlite3

import pytest

from gmail_clean.storage import _MIGRATIONS, Store


def old_database(path):
	with sqlite3.connect(path) as connection:
		for migration in _MIGRATIONS[:6]:
			connection.executescript(migration)
		connection.execute("PRAGMA user_version=6")


def snapshots(path):
	result = []
	for index in range(4):
		backup = Path(str(path) + f".pre-migration-v{index + 1}-old{index}.sqlite3")
		backup.write_bytes(b"historical snapshot")
		os.utime(backup, (100 + index, 100 + index))
		result.append(backup)
	return result


@pytest.mark.parametrize("keep", [1, 2, 3])
def test_successful_upgrade_keeps_newest_snapshots_for_this_database(tmp_path, keep):
	path = tmp_path / "mail.sqlite"
	old_database(path)
	previous = snapshots(path)
	manual = tmp_path / "manual-backup.sqlite3"
	manual.write_bytes(b"manual backup")
	other = tmp_path / "other.sqlite.pre-migration-v1-old.sqlite3"
	other.write_bytes(b"other database")
	with Store(path, "me@example.invalid", migration_backup_count=keep):
		remaining = list(tmp_path.glob("mail.sqlite.pre-migration-*.sqlite3"))
		assert len(remaining) == keep
		assert all(not backup.exists() for backup in previous[:5 - keep])
		assert all(backup.exists() for backup in previous[5 - keep:])
		newest = next(backup for backup in remaining if "-v6-" in backup.name)
		with sqlite3.connect(newest) as connection:
			assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
	assert manual.exists() and other.exists()


def test_failed_upgrade_retains_all_recovery_snapshots(tmp_path, monkeypatch):
	path = tmp_path / "mail.sqlite"
	old_database(path)
	previous = snapshots(path)
	monkeypatch.setattr("gmail_clean.storage._MIGRATIONS", [*_MIGRATIONS[:6], "INVALID SQL;"])
	with pytest.raises(sqlite3.DatabaseError):
		Store(path, "me@example.invalid")
	assert all(backup.exists() for backup in previous)
	assert len(list(tmp_path.glob("mail.sqlite.pre-migration-*.sqlite3"))) == 5
	with sqlite3.connect(path) as connection:
		assert connection.execute("PRAGMA user_version").fetchone()[0] == 6


def test_migration_retention_requires_a_positive_count(tmp_path):
	with pytest.raises(ValueError, match="at least one"):
		Store(tmp_path / "db", "me@example.invalid", migration_backup_count=0)
