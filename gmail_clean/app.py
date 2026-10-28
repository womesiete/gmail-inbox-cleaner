"""Windows/PyCharm launch entry points and an account-free CLI demo."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sys
import time
from pathlib import Path

from .domain import Config, content_hash
from .runtime import data_directory, utc_now
from .service import AppService
from .storage import Store


def parser():
	result = argparse.ArgumentParser(description="Gmail Inbox Cleaner — simulation by default")
	result.add_argument("--demo", action="store_true", help="Use a separate, network-free synthetic mailbox")
	result.add_argument("--headless", action="store_true", help="Run one durable job without the desktop interface")
	result.add_argument("--data-dir", type=Path, help="Data root; separate demo/live subdirectories are added")
	result.add_argument("--count", type=int, default=3000, help="Synthetic message count for a new demo mailbox")
	result.add_argument("--benchmark", type=int, metavar="ROWS", help="Generate and measure SQL table paging; offline only")
	result.add_argument("--resume", metavar="JOB_ID", help="Resume the specified durable job")
	result.add_argument("--ai-module", help="External package name or standalone Python file")
	result.add_argument("--model", help="Exact external model identifier; empty uses the module default")
	result.add_argument("--credentials", type=Path, help="Installed-desktop OAuth client JSON for explicit connection")
	result.add_argument("--live", action="store_true", help="Explicitly enable mailbox writes for this invocation")
	result.add_argument("--max-actions", type=int, help="Maximum mailbox actions authorized for this invocation")
	result.add_argument("--yes", action="store_true", help="Confirm live execution, including a plan above the configured write cap")
	result.add_argument("--apply", action="store_true", help="Apply current approved proposals (requires live mode)")
	result.add_argument("--backup", type=Path, help="Save a consistent SQLite backup after the run")
	return result


def benchmark(count: int, directory: Path) -> dict:
	"""Measure the real SQL model at scale; report timings rather than a promise."""
	from .demo import generate_messages
	from .table_model import Column, SqlTableModel
	from PyQt6.QtCore import QCoreApplication
	application = QCoreApplication.instance() or QCoreApplication([])
	# Keep an owned Qt application alive for the whole model measurement.
	assert application is not None
	path = directory / f"scale-{count}.sqlite"
	store = Store(path, "demo@example.invalid")
	start = time.perf_counter()
	with store.transaction():
		for message in generate_messages(count, utc_now()):
			message.content_hash = content_hash(message)
			store.upsert_message(message)
	seed_seconds = time.perf_counter() - start
	start = time.perf_counter()
	model = SqlTableModel(path, "SELECT id,sender,subject,internal_ms,decision FROM messages", [Column("subject", "Subject"), Column("internal_ms", "Date")])
	first = model.record(0)
	first_seconds = time.perf_counter() - start
	start = time.perf_counter()
	last = model.record(max(0, model.rowCount() - 1))
	last_seconds = time.perf_counter() - start
	for index in range(0, model.rowCount(), 1024):
		model.record(index)
	result = {"rows": model.rowCount(), "seed_seconds": round(seed_seconds, 4), "first_page_seconds": round(first_seconds, 6), "last_page_seconds": round(last_seconds, 6), "cached_pages": len(model._pages), "max_pages": model.max_pages, "first_id": first.get("id"), "last_id": last.get("id"), "database": str(path)}
	model.close()
	store.close()
	return result


def run_until_settled(service, job_id, progress=None, *, should_stop=None, sleep=time.sleep, max_wait_seconds=300):
	"""Resume durable operational retries within an interruptible wait budget.

	User pauses and authorization blocks require an explicit user action. Quota
	resets outside this invocation's wait budget remain visible for a later run.
	"""
	summary = service.run_job(job_id, progress=progress, should_stop=should_stop)
	waited = 0.0
	while summary["state"] in {"waiting", "retry", "paused"}:
		if should_stop and should_stop():
			signal = should_stop()
			service.control_job(job_id, signal if signal in {"paused", "stopped"} else "paused")
			return service.jobs.summary(job_id)
		due = service.jobs.retry_deadline(job_id)
		if due is None:
			return service.jobs.summary(job_id)
		delay = max(0.0, (due - service.clock()).total_seconds())
		if delay > max(0.0, max_wait_seconds - waited):
			return service.jobs.summary(job_id)
		if delay > 0:
			if progress:
				progress(f"Waiting for scheduled retry at {due.isoformat()}")
			interval = min(delay, 30)
			sleep(interval)
			waited += interval
			continue
		summary = service.run_job(job_id, progress=progress, should_stop=should_stop)
	return summary


def main(argv=None) -> int:
	multiprocessing.freeze_support()
	args = parser().parse_args(argv)
	if args.count < 1 or (args.benchmark is not None and args.benchmark < 1) or (args.max_actions is not None and args.max_actions < 1):
		parser().error("Counts must be positive")
	if args.headless and args.live and args.max_actions is None and not args.yes:
		parser().error("Headless --live requires --max-actions N or --yes")
	directory = args.data_dir / ("demo" if args.demo else "live") if args.data_dir else data_directory(args.demo)
	directory.mkdir(parents=True, exist_ok=True)
	if args.benchmark:
		if not args.demo:
			parser().error("--benchmark requires --demo")
		print(json.dumps(benchmark(args.benchmark, directory), indent=2))
		return 0
	service = None
	try:
		if args.headless:
			from .gui import acquire_instance_lock
			lock = acquire_instance_lock(directory)
			service = AppService(data_dir=directory, demo=args.demo, count=args.count)
			# A console run always requires its own explicit --live choice, even
			# when the desktop's saved preferences allow automatic execution.
			service.set_session_config(dry_run=not args.live)
			service._session_write_confirmation = args.yes
			if args.max_actions is not None:
				service.set_session_config(max_actions_per_run=args.max_actions)
		else:
			from PyQt6.QtCore import QSettings
			from PyQt6.QtWidgets import QApplication
			from .gui import MainWindow, acquire_instance_lock, load_preferences
			application = QApplication(sys.argv[:1])
			application.setApplicationName("Gmail Inbox Cleaner")
			application.setOrganizationName("GmailInboxCleaner")
			lock = acquire_instance_lock(directory)
			settings = QSettings("GmailInboxCleaner", "Demo" if args.demo else "Personal")
			config = load_preferences(settings)
			service = AppService(config=config, data_dir=directory, demo=args.demo, count=args.count)
		if args.ai_module is not None or args.model is not None or args.live:
			config = Config.from_dict(service.config.to_dict())
			if args.ai_module is not None:
				config.ai_module = args.ai_module
			if args.model is not None:
				config.ai_model = args.model
			if args.live:
				config.dry_run = False
			service.set_session_config(**{name: getattr(config, name) for name in ("ai_module", "ai_model", "dry_run")})
		if args.credentials:
			service.connect(args.credentials)
		if args.headless:
			job_id = args.resume or service.new_job()
			last_report = [0.0, ""]
			def report(value):
				stage = value.partition(":")[0]
				now = time.monotonic()
				if stage != last_report[1] or now - last_report[0] >= 0.5:
					print(value, flush=True)
					last_report[:] = [now, stage]
			summary = run_until_settled(service, job_id, progress=report)
			if args.apply and summary["state"] == "completed":
				if service.config.dry_run:
					raise ValueError("Enable live mode explicitly before applying proposals")
				snapshot = service.plan_snapshot(job_id=job_id)
				summary["execution_plan"] = snapshot
				summary["execution"] = service.apply_actions(job_id=job_id, expected_plan_hash=snapshot["hash"], allow_over_limit=args.yes)
			if args.backup:
				summary["backup"] = service.backup(args.backup)
			print(json.dumps(summary, indent=2))
			return 0 if summary["state"] == "completed" and not summary.get("failed", 0) else 2
		window = MainWindow(service, settings=settings)
		window.show()
		code = application.exec()
		lock.unlock()
		if window.shutdown_incomplete and argv is None:
			# Qt cannot safely kill a thread blocked in an external network call.
			# A standalone application's explicit close deadline ends its process
			# after local cleanup. WAL and pre-write attempts recover on next launch;
			# an external Gmail/provider request may still have completed remotely.
			service.close()
			(directory / "shutdown-notice.log").write_text(
				"Window close deadline reached with unfinished network work. Inspect interrupted jobs and reconcile uncertain actions on next launch. Remote requests may still finish or be billed.\n",
				encoding="utf-8")
			for stream in (sys.stdout, sys.stderr):
				if stream is not None:
					stream.flush()
			os._exit(code)
		return code
	except KeyboardInterrupt:
		if args.headless:
			print("Interrupted; completed work and retry checkpoints are retained.", file=sys.stderr)
		return 130
	except Exception as exc:
		from .runtime import safe_error
		message = safe_error(exc)
		if isinstance(exc, ValueError):
			message = str(exc)
		if args.headless:
			print(message, file=sys.stderr)
		else:
			try:
				from PyQt6.QtWidgets import QApplication, QMessageBox
				application = QApplication.instance() or QApplication(sys.argv[:1])
				QMessageBox.critical(None, "Gmail Inbox Cleaner", message)
			except Exception:
				# pythonw has no console; keep a bounded content-free startup diagnostic.
				(directory / "startup-error.log").write_text(message, encoding="utf-8")
		return 1
	finally:
		if service is not None:
			service.close()


if __name__ == "__main__":
	raise SystemExit(main())
