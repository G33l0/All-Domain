"""Database-backed domain browsing for the desktop app.

Every query runs on a worker thread that owns its own read-only connection;
results reach the UI through queued signals. Rows are pulled a page at a time
via ``canFetchMore``/``fetchMore``, so opening a database of any size costs one
page read.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QRunnable,
    Qt,
    QThread,
    Signal,
    Slot,
)

from ..query import (
    ORDER_NEWEST,
    Cursor,
    DomainFilter,
    DomainQuery,
    DomainRow,
    QueryError,
)

PAGE_SIZE = 250
COUNT_CAP = 20_000


class DatabaseBrowser(QObject):
    """Runs read-only queries off the GUI thread."""

    page_ready = Signal(int, list, bool)
    count_ready = Signal(int, int, bool)
    facets_ready = Signal(list, list)
    summary_ready = Signal(dict)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._query: Optional[DomainQuery] = None
        self._db_path = ""

    @Slot(str)
    def open(self, db_path: str) -> None:
        self.close()
        self._db_path = db_path
        if not self._ensure():
            self.failed.emit(f"no database at {db_path}")

    def _ensure(self) -> bool:
        """Open the database if it exists now.

        The collector creates the file on first run, so a browser started
        before any collection has happened must be able to attach later.
        """
        if self._query is not None:
            return True
        if not self._db_path:
            return False
        try:
            self._query = DomainQuery(self._db_path).open()
        except QueryError:
            self._query = None
            return False
        return True

    @Slot()
    def close(self) -> None:
        if self._query is not None:
            self._query.close()
            self._query = None

    @Slot(int, object, object, str)
    def fetch_page(self, request_id: int, criteria: DomainFilter,
                   after: Optional[Cursor], order: str) -> None:
        if not self._ensure():
            self.page_ready.emit(request_id, [], False)
            return
        try:
            rows = self._query.page(criteria, limit=PAGE_SIZE, after=after, order=order)
        except QueryError as exc:
            self.failed.emit(str(exc))
            rows = []
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            rows = []
        self.page_ready.emit(request_id, rows, len(rows) == PAGE_SIZE)

    @Slot(int, object)
    def fetch_count(self, request_id: int, criteria: DomainFilter) -> None:
        if not self._ensure():
            self.count_ready.emit(request_id, 0, False)
            return
        try:
            total, capped = self._query.count(criteria, cap=COUNT_CAP)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            total, capped = 0, False
        self.count_ready.emit(request_id, total, capped)

    @Slot()
    def fetch_facets(self) -> None:
        if not self._ensure():
            return
        try:
            technologies = [name for name, _count in self._query.technologies(300)]
            sources = self._query.sources()
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.facets_ready.emit(technologies, sources)

    @Slot()
    def fetch_summary(self) -> None:
        if not self._ensure():
            return
        try:
            self.summary_ready.emit(self._query.summary())
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class StoredDomainModel(QAbstractTableModel):
    """Incrementally loaded view of the stored domains."""

    COLUMNS = ("Domain", "Status", "HTTP", "Source", "First seen", "Technologies")
    ROLE_RESPONSIVE = int(Qt.ItemDataRole.UserRole) + 1

    request_page = Signal(int, object, object, str)
    request_count = Signal(int, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: List[DomainRow] = []
        self._filter = DomainFilter()
        self._order = ORDER_NEWEST
        self._has_more = False
        self._loading = False
        self._generation = 0
        self.total = 0
        self.total_capped = False

    # ------------------------------------------------------------- Qt model
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return section + 1

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()
        if role == self.ROLE_RESPONSIVE:
            return row.responsive
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return row.fingerprint
            if column == 1:
                return "Live" if row.responsive else "Down"
            if column == 2:
                return str(row.status_code) if row.status_code is not None else "-"
            if column == 3:
                return row.source or "-"
            if column == 4:
                return (row.first_seen or "")[:19]
            if column == 5:
                return ", ".join(row.technologies) if row.technologies else "-"
        if role == Qt.ItemDataRole.ToolTipRole:
            detail = ", ".join(row.technologies) or "no technologies detected"
            error = f"\nError: {row.error}" if row.error else ""
            return f"{row.fingerprint}\n{detail}{error}"
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (1, 2):
            return int(Qt.AlignmentFlag.AlignCenter)
        return None

    def canFetchMore(self, parent=QModelIndex()) -> bool:
        return not parent.isValid() and self._has_more and not self._loading

    def fetchMore(self, parent=QModelIndex()) -> None:
        if parent.isValid() or self._loading or not self._has_more:
            return
        self._loading = True
        after = DomainQuery.cursor_for(self._rows[-1]) if self._rows else None
        self.request_page.emit(self._generation, self._filter, after, self._order)

    # --------------------------------------------------------------- control
    def reload(self, criteria: Optional[DomainFilter] = None,
               order: Optional[str] = None) -> None:
        """Discard what is loaded and fetch the first page of a new query."""
        if criteria is not None:
            self._filter = criteria
        if order is not None:
            self._order = order
        self._generation += 1
        self.beginResetModel()
        self._rows = []
        self.endResetModel()
        self._has_more = False
        self._loading = True
        self.total = 0
        self.total_capped = False
        self.request_page.emit(self._generation, self._filter, None, self._order)
        self.request_count.emit(self._generation, self._filter)

    @Slot(int, list, bool)
    def on_page(self, request_id: int, rows: Sequence[DomainRow], has_more: bool) -> None:
        if request_id != self._generation:
            return  # a newer query superseded this one
        self._loading = False
        self._has_more = has_more
        if not rows:
            return
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self._rows.extend(rows)
        self.endInsertRows()

    @Slot(int, int, bool)
    def on_count(self, request_id: int, total: int, capped: bool) -> None:
        if request_id != self._generation:
            return
        self.total = total
        self.total_capped = capped

    @property
    def filter(self) -> DomainFilter:
        return self._filter

    @property
    def order(self) -> str:
        return self._order

    def rows(self) -> List[DomainRow]:
        return list(self._rows)

    def loaded_count(self) -> int:
        return len(self._rows)


def start_browser(db_path: str) -> Tuple[QThread, DatabaseBrowser]:
    """Create the worker thread and its browser, already opened on *db_path*."""
    thread = QThread()
    thread.setObjectName("domain-browser")
    browser = DatabaseBrowser()
    browser.moveToThread(thread)
    thread.start()
    return thread, browser


class ExportSignals(QObject):
    finished = Signal(int, str)
    failed = Signal(str)


class ExportTask(QRunnable):
    """Streams an export to disk on a pool thread."""

    def __init__(self, db_path: str, destination: str,
                 criteria: DomainFilter, export_format: str) -> None:
        super().__init__()
        self.db_path = db_path
        self.destination = destination
        self.criteria = criteria
        self.export_format = export_format
        self.signals = ExportSignals()

    def run(self) -> None:
        try:
            from ..export import export_to_path

            written = export_to_path(
                self.db_path, self.destination, self.criteria, self.export_format
            )
        except Exception as exc:
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.signals.finished.emit(written, self.destination)
