"""Table models for the Qt front-end.

Both models are bounded ring buffers: a collector left running for days must
not grow the UI's memory without limit.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List, Tuple

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

from ..engine import Event


class DomainTableModel(QAbstractTableModel):
    """Live feed of probed domains, newest first."""

    COLUMNS = ("Domain", "Status", "HTTP", "Source", "Technologies")
    ROLE_RESPONSIVE = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, max_rows: int = 5000, parent=None) -> None:
        super().__init__(parent)
        self.max_rows = max(1, int(max_rows))
        self._rows: Deque[Dict[str, Any]] = deque(maxlen=self.max_rows)

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
            return row["responsive"]
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return row["domain"]
            if column == 1:
                return "Live" if row["responsive"] else "Down"
            if column == 2:
                return str(row["status"]) if row["status"] is not None else "-"
            if column == 3:
                return row["source"]
            if column == 4:
                return ", ".join(row["technologies"]) if row["technologies"] else "-"
        if role == Qt.ItemDataRole.ToolTipRole:
            technologies = ", ".join(row["technologies"]) or "none detected"
            return f"{row['domain']}\nTechnologies: {technologies}"
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (1, 2):
            return int(Qt.AlignmentFlag.AlignCenter)
        return None

    # ---------------------------------------------------------------- writes
    def add_events(self, events: List[Event]) -> None:
        """Insert a batch of ``domain`` events in one go."""
        payloads = [event.data for event in events if event.kind == "domain" and event.data]
        if not payloads:
            return
        self.beginResetModel()
        for payload in payloads:
            self._rows.appendleft(
                {
                    "domain": payload.get("domain", ""),
                    "responsive": bool(payload.get("responsive")),
                    "status": payload.get("status"),
                    "source": payload.get("source", ""),
                    "technologies": list(payload.get("technologies") or []),
                    "recheck": bool(payload.get("recheck")),
                }
            )
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._rows.clear()
        self.endResetModel()

    def rows(self) -> List[Dict[str, Any]]:
        return list(self._rows)


class TechnologyTableModel(QAbstractTableModel):
    """Technology counts with a share-of-total column for the bar delegate."""

    COLUMNS = ("Technology", "Domains", "Share")
    ROLE_SHARE = int(Qt.ItemDataRole.UserRole) + 2

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: List[Tuple[str, int]] = []
        self._total = 0

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
        name, count = self._rows[index.row()]
        if role == self.ROLE_SHARE:
            return count / self._total if self._total else 0.0
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return name
            if index.column() == 1:
                return count
            return ""
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() == 1:
            return int(Qt.AlignmentFlag.AlignCenter)
        return None

    def set_counts(self, counts: Dict[str, int]) -> None:
        """Replace the whole table, sorted by count then name."""
        rows = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
        if rows == self._rows:
            return
        self.beginResetModel()
        self._rows = rows
        self._total = max(rows[0][1], 1) if rows else 0
        self.endResetModel()

    def clear(self) -> None:
        self.set_counts({})

    def rows(self) -> List[Tuple[str, int]]:
        return list(self._rows)


class DomainFilterProxy(QSortFilterProxyModel):
    """Text + "live only" filtering that survives model resets.

    Hiding rows on the view directly does not work here: the source model
    resets whenever a batch of results arrives, which clears the view's hidden
    rows, so an active filter would silently stop applying.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._needle = ""
        self._live_only = False
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def _refresh(self) -> None:
        # invalidate() re-runs the filter and is the only spelling that is not
        # deprecated across the Qt 6 line.
        self.invalidate()

    def set_needle(self, text: str) -> None:
        self._needle = (text or "").strip().lower()
        self._refresh()

    def set_live_only(self, enabled: bool) -> None:
        self._live_only = bool(enabled)
        self._refresh()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return True
        rows = model.rows()
        if source_row >= len(rows):
            return True
        row = rows[source_row]
        if self._live_only and not row["responsive"]:
            return False
        if not self._needle:
            return True
        haystack = f"{row['domain']} {' '.join(row['technologies'])} {row['source']}".lower()
        return self._needle in haystack


class TechnologyFilterProxy(QSortFilterProxyModel):
    """Name filtering for the technology table."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._needle = ""

    def _refresh(self) -> None:
        self.invalidate()

    def set_needle(self, text: str) -> None:
        self._needle = (text or "").strip().lower()
        self._refresh()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._needle:
            return True
        model = self.sourceModel()
        if model is None:
            return True
        rows = model.rows()
        if source_row >= len(rows):
            return True
        return self._needle in rows[source_row][0].lower()
