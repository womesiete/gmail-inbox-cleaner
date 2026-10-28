"""Allow python/pythonw -m gmail_clean with Windows-spawn-safe startup."""

from .app import main

if __name__ == "__main__":
	raise SystemExit(main())
