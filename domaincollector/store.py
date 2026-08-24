"""Persistence layer: SQLite database plus per-technology text files.

Design notes
------------
* One long-lived ``aiosqlite`` connection is used instead of opening a new
  connection per domain (the 1.x behaviour), which was both slow and a source
  of ``database is locked`` errors under concurrency.
* WAL journalling and a busy timeout make concurrent readers safe.
* Writes are batched by a background task and flushed with ``executemany``.
* ``reserve()`` claims a fingerprint in memory before it is queued so the same
  domain can never be probed twice by two workers.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

import aiosqlite

SCHEMA_VERSION = 2

_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9 ._+-]")
_RESERVED_WINDOWS_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def safe_filename(name: str, fallback: str = "unknown") -> str:
    """Turn a technology name into a file name that is safe on every OS."""
    cleaned = _UNSAFE_FILENAME_RE.sub("_", str(name)).strip(" ._")
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    if not cleaned:
        return fallback
    if cleaned.lower() in _RESERVED_WINDOWS_NAMES:
        cleaned = f"{cleaned}_tech"
    return cleaned[:100]


@dataclass
class DomainRecord:
    """One probed domain, ready to be persisted."""

    fingerprint: str
    raw: str
    responsive: bool = False
    technologies: List[str] = field(default_factory=list)
    versions: Dict[str, str] = field(default_factory=dict)
    status_code: Optional[int] = None
    scheme: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
    source: Optional[str] = None


class DomainStore:
    """Async SQLite-backed store with in-memory deduplication."""

    def __init__(
        self,
        db_path: str = "domains.db",
        output_dir: str = "output",
        write_tech_files: bool = True,
        flush_size: int = 50,
        flush_interval: float = 1.0,
    ) -> None:
        self.db_path = db_path
        self.output_dir = output_dir
        self.write_tech_files = write_tech_files
        self.flush_size = max(1, int(flush_size))
        self.flush_interval = max(0.05, float(flush_interval))

        self._db: Optional[aiosqlite.Connection] = None
        self._seen: Set[str] = set()
        self._reserved: Set[str] = set()
        self._pending: List[DomainRecord] = []
        self._flush_lock = asyncio.Lock()
        self._file_lock = asyncio.Lock()
        self._flusher: Optional[asyncio.Task] = None
        self._closed = False

    # ------------------------------------------------------------- lifecycle
    async def open(self) -> "DomainStore":
        """Create/upgrade the schema, load known fingerprints, start the flusher."""
        directory = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(directory, exist_ok=True)
        if self.write_tech_files:
            os.makedirs(self.output_dir, exist_ok=True)

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=10000")
        await self._migrate()
        async with self._db.execute("SELECT fingerprint FROM domains") as cursor:
            self._seen = {row[0] async for row in cursor}
        self._closed = False
        self._flusher = asyncio.create_task(self._flush_loop(), name="store-flusher")
        return self

    async def close(self) -> None:
        """Flush everything still buffered and close the connection."""
        self._closed = True
        if self._flusher is not None:
            self._flusher.cancel()
            try:
                await self._flusher
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._flusher = None
        try:
            await self.flush()
        finally:
            if self._db is not None:
                try:
                    await self._db.close()
                finally:
                    self._db = None

    async def __aenter__(self) -> "DomainStore":
        return await self.open()

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    # ---------------------------------------------------------------- schema
    async def _migrate(self) -> None:
        assert self._db is not None
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS domains (
                fingerprint TEXT PRIMARY KEY,
                raw         TEXT NOT NULL,
                first_seen  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                responsive  INTEGER NOT NULL DEFAULT 0,
                technologies TEXT
            )
            """
        )
        existing = set()
        async with self._db.execute("PRAGMA table_info(domains)") as cursor:
            async for row in cursor:
                existing.add(row[1])
        for column, ddl in (
            ("status_code", "ALTER TABLE domains ADD COLUMN status_code INTEGER"),
            ("scheme", "ALTER TABLE domains ADD COLUMN scheme TEXT"),
            ("error", "ALTER TABLE domains ADD COLUMN error TEXT"),
            ("elapsed_ms", "ALTER TABLE domains ADD COLUMN elapsed_ms INTEGER"),
            ("source", "ALTER TABLE domains ADD COLUMN source TEXT"),
            ("checked_at", "ALTER TABLE domains ADD COLUMN checked_at TIMESTAMP"),
        ):
            if column not in existing:
                await self._db.execute(ddl)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS domain_tech (
                fingerprint TEXT NOT NULL,
                technology  TEXT NOT NULL,
                version     TEXT,
                PRIMARY KEY (fingerprint, technology)
            )
            """
        )
        tech_columns = set()
        async with self._db.execute("PRAGMA table_info(domain_tech)") as cursor:
            async for row in cursor:
                tech_columns.add(row[1])
        if "version" not in tech_columns:
            await self._db.execute("ALTER TABLE domain_tech ADD COLUMN version TEXT")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_domains_responsive ON domains(responsive)")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_domain_tech_tech ON domain_tech(technology)")
        await self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        await self._db.commit()

    # ------------------------------------------------------------ dedup logic
    def is_known(self, fingerprint: str) -> bool:
        """``True`` if the domain is already stored or already queued."""
        return fingerprint in self._seen or fingerprint in self._reserved

    def reserve(self, fingerprint: str) -> bool:
        """Claim a fingerprint for processing.  ``False`` when already claimed."""
        if self.is_known(fingerprint):
            return False
        self._reserved.add(fingerprint)
        return True

    def release(self, fingerprint: str) -> None:
        """Give a reservation back (used when a domain is dropped unprocessed)."""
        self._reserved.discard(fingerprint)

    @property
    def known_count(self) -> int:
        return len(self._seen)

    # ---------------------------------------------------------------- writing
    async def add(self, record: DomainRecord) -> bool:
        """Buffer *record* for insertion.  ``False`` when it is a duplicate."""
        if record.fingerprint in self._seen:
            self._reserved.discard(record.fingerprint)
            return False
        self._seen.add(record.fingerprint)
        self._reserved.discard(record.fingerprint)
        self._pending.append(record)
        if len(self._pending) >= self.flush_size:
            await self.flush()
        return True

    async def _flush_loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(self.flush_interval)
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let a flush failure kill the background task.
                await asyncio.sleep(self.flush_interval)

    async def flush(self) -> int:
        """Write buffered records to SQLite and the technology files."""
        async with self._flush_lock:
            if not self._pending:
                return 0
            batch, self._pending = self._pending, []
            if self._db is None:
                return 0
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            rows = [
                (
                    record.fingerprint,
                    record.raw,
                    1 if record.responsive else 0,
                    json.dumps(record.technologies) if record.technologies else None,
                    record.status_code,
                    record.scheme,
                    record.error,
                    record.elapsed_ms,
                    record.source,
                    now,
                )
                for record in batch
            ]
            tech_rows = [
                (record.fingerprint, technology, record.versions.get(technology) or None)
                for record in batch
                for technology in record.technologies
            ]
            try:
                await self._db.executemany(
                    """
                    INSERT OR IGNORE INTO domains
                        (fingerprint, raw, responsive, technologies, status_code,
                         scheme, error, elapsed_ms, source, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                if tech_rows:
                    await self._db.executemany(
                        "INSERT OR IGNORE INTO domain_tech (fingerprint, technology, version) "
                        "VALUES (?, ?, ?)",
                        tech_rows,
                    )
                await self._db.commit()
            except Exception:
                # Put the batch back so nothing is silently lost.
                self._pending = batch + self._pending
                raise
            if self.write_tech_files:
                await self._write_tech_files(batch)
            return len(batch)

    async def _write_tech_files(self, batch: Sequence[DomainRecord]) -> None:
        grouped: Dict[str, List[str]] = {}
        for record in batch:
            if not record.responsive:
                continue
            for technology in record.technologies:
                grouped.setdefault(safe_filename(technology), []).append(record.raw)
        if not grouped:
            return
        async with self._file_lock:
            await asyncio.to_thread(self._append_files, grouped)

    def _append_files(self, grouped: Dict[str, List[str]]) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        for name, domains in grouped.items():
            path = os.path.join(self.output_dir, f"{name}.txt")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("\n".join(domains) + "\n")

    # ---------------------------------------------------------------- queries
    async def summary(self) -> Dict[str, int]:
        """Totals straight from the database."""
        await self.flush()
        assert self._db is not None
        async with self._db.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(responsive), 0) AS responsive FROM domains"
        ) as cursor:
            row = await cursor.fetchone()
        async with self._db.execute("SELECT COUNT(DISTINCT technology) FROM domain_tech") as cursor:
            tech_row = await cursor.fetchone()
        return {
            "total": int(row["total"] if row else 0),
            "responsive": int(row["responsive"] if row else 0),
            "technologies": int(tech_row[0] if tech_row else 0),
        }

    async def top_technologies(self, limit: int = 20) -> List[Tuple[str, int]]:
        await self.flush()
        assert self._db is not None
        async with self._db.execute(
            "SELECT technology, COUNT(*) AS hits FROM domain_tech "
            "GROUP BY technology ORDER BY hits DESC, technology ASC LIMIT ?",
            (int(limit),),
        ) as cursor:
            return [(row[0], int(row[1])) async for row in cursor]

    async def domains_for_technology(self, technology: str, limit: int = 1000) -> List[str]:
        await self.flush()
        assert self._db is not None
        async with self._db.execute(
            "SELECT d.raw FROM domain_tech t JOIN domains d ON d.fingerprint = t.fingerprint "
            "WHERE t.technology = ? ORDER BY d.raw LIMIT ?",
            (technology, int(limit)),
        ) as cursor:
            return [row[0] async for row in cursor]
