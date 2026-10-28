"""SQL-backed tables with bounded row-page caching.

Only view definitions written by the application become SQL. Search text remains
bound parameters, and sorting is limited to the declared result columns.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThread
from PyQt6.QtGui import QColor


@dataclass(frozen=True)
class Column:
	key: str
	title: str
	width: int = 140
	formatter: Any = None


class SqlTableModel(QAbstractTableModel):
	"""Virtualized read-only result set owned by the GUI thread.

	``query`` must be application-owned SQL with no ORDER BY or LIMIT. Filtering
	can refer to the result aliases by wrapping the query as a subquery. Bodies
	must never be selected for overview tables.
	"""

	def __init__(self, path: str | Path, query: str, columns: list[Column], *,
			id_column: str = "id", page_size: int = 128, max_pages: int = 8,
			parent=None):
		super().__init__(parent)
		self.path = Path(path)
		self.query = query.strip().rstrip(";")
		self.columns = columns
		self.id_column = id_column
		self.page_size = max(16, page_size)
		self.max_pages = max(1, max_pages)
		self._pages: OrderedDict[int, list[sqlite3.Row]] = OrderedDict()
		self._where = "1"
		self._params: tuple[Any, ...] = ()
		self._sort_key = id_column
		self._descending = False
		self._count = 0
		self.last_error = ""
		self.connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
		self.connection.row_factory = sqlite3.Row
		self.connection.execute("PRAGMA busy_timeout = 5000")
		self.refresh()

	def _check_thread(self):
		if QThread.currentThread() != self.thread():
			raise RuntimeError("SQL table models must remain on their owning GUI thread")

	def close(self):
		self._check_thread()
		self.connection.close()

	def _sql(self) -> str:
		return f"SELECT * FROM ({self.query}) AS rows WHERE {self._where}"

	def refresh(self):
		self._check_thread()
		self.beginResetModel()
		self._pages.clear()
		try:
			self._count = self.connection.execute(
				f"SELECT COUNT(*) FROM ({self._sql()})", self._params).fetchone()[0]
			self.last_error = ""
		except sqlite3.Error as exc:
			self._count = 0
			self.last_error = str(exc)
		self.endResetModel()

	def set_filter(self, where: str = "1", params: tuple[Any, ...] = ()):
		"""Set an application-owned WHERE expression with bound user values."""
		self._check_thread()
		self._where, self._params = where, tuple(params)
		self.refresh()

	def rowCount(self, parent=QModelIndex()):
		return 0 if parent.isValid() else self._count

	def columnCount(self, parent=QModelIndex()):
		return 0 if parent.isValid() else len(self.columns)

	def record(self, row: int) -> dict[str, Any]:
		self._check_thread()
		if not 0 <= row < self._count:
			return {}
		page = row // self.page_size
		if page not in self._pages:
			direction = "DESC" if self._descending else "ASC"
			# Identifiers originate exclusively from Column declarations above.
			order = f'"{self._sort_key}" {direction}, "{self.id_column}" ASC'
			self._pages[page] = self.connection.execute(
				f"{self._sql()} ORDER BY {order} LIMIT ? OFFSET ?",
				(*self._params, self.page_size, page * self.page_size)).fetchall()
			while len(self._pages) > self.max_pages:
				self._pages.popitem(last=False)
		self._pages.move_to_end(page)
		rows = self._pages[page]
		index = row % self.page_size
		return dict(rows[index]) if index < len(rows) else {}

	def rows_for_ids(self, identifiers):
		"""Locate an explicit selection after refresh without materializing rows."""
		self._check_thread()
		identifiers = list(dict.fromkeys(identifiers))
		if not identifiers:
			return []
		direction = "DESC" if self._descending else "ASC"
		order = f'"{self._sort_key}" {direction}, "{self.id_column}" ASC'
		placeholders = ",".join("?" for _ in identifiers)
		query = (f'SELECT row_number FROM (SELECT "{self.id_column}" AS row_id, '
			f'ROW_NUMBER() OVER (ORDER BY {order})-1 AS row_number FROM ({self._sql()})) '
			f'WHERE row_id IN ({placeholders})')
		return [row[0] for row in self.connection.execute(query, (*self._params, *identifiers))]

	def data(self, index, role=Qt.ItemDataRole.DisplayRole):
		if not index.isValid() or not 0 <= index.column() < len(self.columns):
			return None
		row = self.record(index.row())
		if role == Qt.ItemDataRole.UserRole:
			return row
		if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
			column = self.columns[index.column()]
			value = row.get(column.key)
			if value is not None and column.formatter is not None:
				return column.formatter(value)
			if value is None:
				return ""
			if isinstance(value, str):
				return value
			return str(value)
		if role == Qt.ItemDataRole.ForegroundRole:
			state = str(row.get("state", row.get("decision", ""))).lower()
			if state in {"failed", "conflict", "stale", "uncertain", "error"}:
				return QColor("#a3372d")
			if state in {"completed", "applied", "executed", "archive"}:
				return QColor("#236747")
		return None

	def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
		if role == Qt.ItemDataRole.DisplayRole:
			if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.columns):
				return self.columns[section].title
			if orientation == Qt.Orientation.Vertical:
				return str(section + 1)
		return None

	def sort(self, column, order=Qt.SortOrder.AscendingOrder):
		if 0 <= column < len(self.columns):
			self._sort_key = self.columns[column].key
			self._descending = order == Qt.SortOrder.DescendingOrder
			self.refresh()
