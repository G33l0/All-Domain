"""Read-only queries over the domain database.

Writes go through the async :class:`~domainatlas.store.DomainStore`; this
module provides synchronous, paged reads for the desktop app and the export
commands. SQLite runs in WAL mode, so readers never block the collector.

Result sets are always paged and counts are capped, so a database holding
millions of rows is browsed at the same speed as an empty one.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

COUNT_CAP = 100_000
PAGE_SIZE = 250

#: Orderings the paged reader supports. Each is backed by an index and read
#: with keyset ("seek") pagination, so page 10,000 costs the same as page 1.
ORDER_NEWEST = "newest"
ORDER_NAME = "name"
ORDERINGS = (ORDER_NEWEST, ORDER_NAME)


class QueryError(RuntimeError):
    """The database could not be read."""


def _escape_like(text: str) -> str:
    """Escape LIKE metacharacters so a search term matches literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class DomainFilter:
    """Criteria for selecting stored domains."""

    text: str = ""
    technology: Optional[str] = None
    source: Optional[str] = None
    responsive: Optional[bool] = None
    status_code: Optional[int] = None
    since: Optional[str] = None
    onion: Optional[bool] = None

    def is_empty(self) -> bool:
        return not any(
            (self.text, self.technology, self.source, self.since)
        ) and self.responsive is None and self.status_code is None and self.onion is None

    def where(self) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if self.text:
            clauses.append("d.fingerprint LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(self.text.strip().lower())}%")
        if self.technology:
            clauses.append(
                "EXISTS (SELECT 1 FROM domain_tech t "
                "WHERE t.fingerprint = d.fingerprint AND t.technology = ?)"
            )
            params.append(self.technology)
        if self.source:
            clauses.append("d.source = ?")
            params.append(self.source)
        if self.responsive is not None:
            clauses.append("d.responsive = ?")
            params.append(1 if self.responsive else 0)
        if self.status_code is not None:
            clauses.append("d.status_code = ?")
            params.append(int(self.status_code))
        if self.since:
            clauses.append("COALESCE(d.checked_at, d.first_seen) >= ?")
            params.append(self.since)
        if self.onion is not None:
            clauses.append(
                "d.fingerprint LIKE '%.onion'" if self.onion else "d.fingerprint NOT LIKE '%.onion'"
            )
        return (" AND ".join(clauses) if clauses else "1=1"), params


@dataclass
class Cursor:
    """Position of the last row read, for keyset pagination."""

    first_seen: Optional[str] = None
    fingerprint: str = ""


@dataclass
class DomainRow:
    """One stored domain, flattened for display and export."""

    fingerprint: str
    responsive: bool
    status_code: Optional[int]
    scheme: Optional[str]
    source: Optional[str]
    error: Optional[str]
    first_seen: Optional[str]
    checked_at: Optional[str]
    technologies: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.fingerprint,
            "responsive": self.responsive,
            "status_code": self.status_code,
            "scheme": self.scheme,
            "source": self.source,
            "error": self.error,
            "first_seen": self.first_seen,
            "checked_at": self.checked_at,
            "technologies": list(self.technologies),
        }


def _row_to_domain(row: sqlite3.Row) -> DomainRow:
    raw_technologies = row["technologies"]
    technologies: List[str] = []
    if raw_technologies:
        try:
            parsed = json.loads(raw_technologies)
            if isinstance(parsed, list):
                technologies = [str(item) for item in parsed]
        except (json.JSONDecodeError, TypeError):
            technologies = []
    return DomainRow(
        fingerprint=row["fingerprint"],
        responsive=bool(row["responsive"]),
        status_code=row["status_code"],
        scheme=row["scheme"],
        source=row["source"],
        error=row["error"],
        first_seen=row["first_seen"],
        checked_at=row["checked_at"],
        technologies=technologies,
    )


class DomainQuery:
    """A read-only handle on a domain database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._connection: Optional[sqlite3.Connection] = None
        self._has_undated = False

    def open(self) -> "DomainQuery":
        if not os.path.exists(self.db_path):
            raise QueryError(f"no database at {self.db_path}")
        try:
            uri = f"file:{os.path.abspath(self.db_path)}?mode=ro"
            self._connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA query_only=1")
            self._connection.execute("PRAGMA busy_timeout=5000")
            # Databases written before first_seen was backfilled may contain
            # undated rows; paging only pays for that case when it exists.
            self._has_undated = bool(
                self._connection.execute(
                    "SELECT 1 FROM domains WHERE first_seen IS NULL LIMIT 1"
                ).fetchone()
            )
        except sqlite3.Error as exc:
            raise QueryError(f"cannot open {self.db_path}: {exc}") from exc
        return self

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "DomainQuery":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise QueryError("query handle is not open")
        return self._connection

    def page(
        self,
        criteria: Optional[DomainFilter] = None,
        limit: int = PAGE_SIZE,
        after: Optional[Cursor] = None,
        order: str = ORDER_NEWEST,
    ) -> List[DomainRow]:
        """One page of matching domains, continuing after *after*.

        Pagination is keyset based rather than OFFSET based: OFFSET makes
        SQLite walk every skipped row, which turns deep scrolling into a
        multi-second stall on a large database.
        """
        criteria = criteria or DomainFilter()
        where, params = criteria.where()
        params = list(params)

        if order == ORDER_NAME:
            if after is not None and after.fingerprint:
                where += " AND d.fingerprint > ?"
                params.append(after.fingerprint)
            order_by = "d.fingerprint ASC"
        else:
            if after is not None and after.fingerprint:
                # Rows are ordered timestamped-first, then undated ones. A
                # row-value comparison against NULL yields NULL, so the two
                # groups need separate predicates; a single expression would
                # silently drop every legacy row that has no timestamp.
                if after.first_seen:
                    if self._has_undated:
                        where += (
                            " AND (d.first_seen IS NULL"
                            " OR (d.first_seen, d.fingerprint) < (?, ?))"
                        )
                    else:
                        where += " AND (d.first_seen, d.fingerprint) < (?, ?)"
                    params.extend([after.first_seen, after.fingerprint])
                else:
                    where += " AND d.first_seen IS NULL AND d.fingerprint < ?"
                    params.append(after.fingerprint)
            # SQLite sorts NULL lowest, so a plain DESC already places undated
            # rows last. Adding an "IS NULL" sort key would express the same
            # order but make the index unusable.
            order_by = "d.first_seen DESC, d.fingerprint DESC"

        sql = (
            "SELECT d.fingerprint, d.responsive, d.status_code, d.scheme, d.source, "
            "       d.error, d.first_seen, d.checked_at, d.technologies "
            f"FROM domains d WHERE {where} ORDER BY {order_by} LIMIT ?"
        )
        params.append(int(limit))
        cursor = self.connection.execute(sql, params)
        return [_row_to_domain(row) for row in cursor]

    @staticmethod
    def cursor_for(row: DomainRow) -> Cursor:
        return Cursor(first_seen=row.first_seen, fingerprint=row.fingerprint)

    def iter_all(
        self, criteria: Optional[DomainFilter] = None, batch_size: int = 2000
    ) -> Iterator[DomainRow]:
        """Stream every matching domain, a batch at a time.

        Used by exports so a multi-million row result never has to be held in
        memory at once.
        """
        criteria = criteria or DomainFilter()
        where, params = criteria.where()
        sql = (
            "SELECT d.fingerprint, d.responsive, d.status_code, d.scheme, d.source, "
            "       d.error, d.first_seen, d.checked_at, d.technologies "
            f"FROM domains d WHERE {where} ORDER BY d.fingerprint ASC"
        )
        cursor = self.connection.execute(sql, params)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            for row in rows:
                yield _row_to_domain(row)

    def count(self, criteria: Optional[DomainFilter] = None, cap: int = COUNT_CAP) -> Tuple[int, bool]:
        """``(count, capped)`` - counting stops at *cap* so the UI stays responsive."""
        criteria = criteria or DomainFilter()
        cap = int(cap)

        # A technology-only filter is answered from the technology index
        # instead of testing EXISTS against every domain row.
        if (
            criteria.technology
            and not criteria.text
            and not criteria.source
            and not criteria.since
            and criteria.responsive is None
            and criteria.status_code is None
            and criteria.onion is None
        ):
            sql = "SELECT COUNT(*) FROM (SELECT 1 FROM domain_tech WHERE technology = ? LIMIT ?)"
            total = self.connection.execute(sql, (criteria.technology, cap + 1)).fetchone()[0]
            return (cap, True) if total > cap else (total, False)

        where, params = criteria.where()
        sql = f"SELECT COUNT(*) FROM (SELECT 1 FROM domains d WHERE {where} LIMIT ?)"
        total = self.connection.execute(sql, (*params, cap + 1)).fetchone()[0]
        return (cap, True) if total > cap else (total, False)

    def technologies(self, limit: int = 500) -> List[Tuple[str, int]]:
        sql = (
            "SELECT technology, COUNT(*) AS hits FROM domain_tech "
            "GROUP BY technology ORDER BY hits DESC, technology ASC LIMIT ?"
        )
        return [(row[0], int(row[1])) for row in self.connection.execute(sql, (int(limit),))]

    def sources(self) -> List[str]:
        sql = "SELECT DISTINCT source FROM domains WHERE source IS NOT NULL ORDER BY source"
        return [row[0] for row in self.connection.execute(sql)]

    def summary(self) -> Dict[str, int]:
        row = self.connection.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(responsive), 0) AS responsive FROM domains"
        ).fetchone()
        technologies = self.connection.execute(
            "SELECT COUNT(DISTINCT technology) FROM domain_tech"
        ).fetchone()[0]
        onion = self.connection.execute(
            "SELECT COUNT(*) FROM domains WHERE fingerprint LIKE '%.onion'"
        ).fetchone()[0]
        return {
            "total": int(row["total"]),
            "responsive": int(row["responsive"]),
            "technologies": int(technologies),
            "onion": int(onion),
        }
