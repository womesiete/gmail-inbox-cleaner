"""Worker signals, thread-owned services, and detached-pool draining."""
from __future__ import annotations

from datetime import timezone
import threading

from PyQt6.QtCore import (QObject, QRunnable, Qt, QTimer, pyqtSignal, pyqtSlot)



UTC = timezone.utc

_DRAINING_WORK = []

class _DrainingWork(QObject):
	"""Keep detached pools alive until their own workers release resources.

	A pool's destructor waits for running QRunnables. Closing a window must not
	delete that pool or a worker-owned SQLite connection from the GUI thread.
	This owner drains independently after the UI has stopped accepting results.
	"""
	def __init__(self, pools, tasks):
		super().__init__()
		self.pools, self.tasks = list(pools), dict(tasks)
		self._drained = False
		for pool in self.pools:
			pool.setParent(None)
		self.timer = QTimer(self)
		self.timer.setInterval(100)
		self.timer.timeout.connect(self._check)
		for task in self.tasks.values():
			task.signals.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
		_DRAINING_WORK.append(self)
		self.timer.start()

	@pyqtSlot(str)
	def _finished(self, task_id):
		self.tasks.pop(task_id, None)
		self._check()

	def _check(self):
		if self._drained:
			return
		if any(pool.activeThreadCount() for pool in self.pools):
			return
		self._drained = True
		self.timer.stop()
		self.tasks.clear()
		for pool in self.pools:
			pool.deleteLater()
		self.pools.clear()
		_DRAINING_WORK.remove(self)
		self.deleteLater()


class TaskSignals(QObject):
	progress = pyqtSignal(str, str)
	result = pyqtSignal(str, object)
	error = pyqtSignal(str, str)
	finished = pyqtSignal(str)


class ServiceTask(QRunnable):
	"""The worker owns its service, transport, and SQLite connection."""
	def __init__(self, task_id, service, operation):
		super().__init__()
		self.task_id, self.service, self.operation = task_id, service, operation
		self.simulation_requested = bool(service.config.dry_run)
		self.signals = TaskSignals()
		self.started = threading.Event()
		self.cancelled = threading.Event()

	def run(self):
		self.started.set()
		try:
			if self.cancelled.is_set():
				return
			with self.service.worker() as worker:
				try:
					# A task submitted from simulation can never inherit a newer live
					# preference. This overlay stays in this worker and never forces live.
					if self.simulation_requested and callable(getattr(worker, "force_session_simulation", None)):
						worker.force_session_simulation()
					result = self.operation(worker, lambda text: self.signals.progress.emit(self.task_id, str(text)))
				except Exception as exc:
					if hasattr(worker, "error"):
						try:
							worker.error("interface", exc)
						except Exception:  # noqa: S110,BLE001 - secondary reporting failure must not mask the original task error; secondary error reporting must preserve the original task exception
							pass  # Failure reporting must preserve the original exception.
					raise
			self.signals.result.emit(self.task_id, result)
		except Exception as exc:  # noqa: BLE001 - Qt worker boundary emits only a sanitized failure signal
			# Raw exception payloads may contain provider request text or credentials.
			from .runtime import safe_error
			self.signals.error.emit(self.task_id, safe_error(exc))
		finally:
			self.signals.finished.emit(self.task_id)
