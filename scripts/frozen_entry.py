"""Absolute-import entry point for PyInstaller and Windows spawned AI workers."""
import multiprocessing

from gmail_clean.app import main


if __name__ == "__main__":
	multiprocessing.freeze_support()
	raise SystemExit(main())
