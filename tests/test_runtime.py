"""Mode-separated per-user application directories on supported platforms."""

from pathlib import Path

from gmail_clean import runtime


def test_windows_local_app_data_takes_precedence_and_modes_remain_separate(tmp_path, monkeypatch):
	monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Windows"))
	monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
	monkeypatch.setattr(runtime.sys, "platform", "darwin")
	assert runtime.data_directory() == tmp_path / "Windows" / "GmailInboxCleaner" / "live"
	assert runtime.data_directory(True) == tmp_path / "Windows" / "GmailInboxCleaner" / "demo"


def test_mac_uses_application_support(tmp_path, monkeypatch):
	monkeypatch.delenv("LOCALAPPDATA", raising=False)
	monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "ignored-xdg"))
	monkeypatch.setattr(runtime.sys, "platform", "darwin")
	monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
	assert runtime.data_directory() == tmp_path / "Library" / "Application Support" / "GmailInboxCleaner" / "live"


def test_linux_respects_xdg_and_empty_environment_values_use_home(tmp_path, monkeypatch):
	monkeypatch.setenv("LOCALAPPDATA", "")
	monkeypatch.setattr(runtime.sys, "platform", "linux")
	monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
	monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
	assert runtime.data_directory(True) == tmp_path / "xdg" / "GmailInboxCleaner" / "demo"
	monkeypatch.setenv("XDG_DATA_HOME", "")
	assert runtime.data_directory() == tmp_path / ".local" / "share" / "GmailInboxCleaner" / "live"
