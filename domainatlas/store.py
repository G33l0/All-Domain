"""Persistence: SQLite database plus per-technology text files.

A single long-lived connection runs in WAL mode with a busy timeout, so readers
never block the writer. Writes are batched by a background task and flushed
with ``executemany``. ``reserve()`` claims a fingerprint before it is queued so
no domain is probed twice.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

import aiosqlite

from .domains import site_of

SCHEMA_VERSION = 3

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
    #: True when this record replaces an existing row (a scheduled re-check).
    recheck: bool = False
    #: False for a name that was recorded without being connected to. Such a
    #: row keeps a null checked_at so a later pass treats it as never checked.
    probed: bool = True


class DomainStore:
    """Async SQLite-backed store with in-memory deduplication."""

    #: Fingerprints kept in memory for fast duplicate rejection. Beyond this
    #: the store falls back to querying SQLite, so a large inventory does not
    #: hold the whole key space in RAM.
    CACHE_LIMIT = 250_000

    def __init__(
        self,
        db_path: str = "domains.db",
        output_dir: str = "output",
        write_tech_files: bool = True,
        flush_size: int = 50,
        flush_interval: float = 1.0,
        cache_limit: Optional[int] = None,
        frontier_limit: int = 500_000,
    ) -> None:
        self.db_path = db_path
        self.output_dir = output_dir
        self.write_tech_files = write_tech_files
        self.flush_size = max(1, int(flush_size))
        self.flush_interval = max(0.05, float(flush_interval))
        self.cache_limit = self.CACHE_LIMIT if cache_limit is None else max(0, int(cache_limit))
        self.frontier_limit = max(0, int(frontier_limit))

        self._db: Optional[aiosqlite.Connection] = None
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._stored_count = 0
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
        async with self._db.execute("SELECT COUNT(*) FROM domains") as cursor:
            row = await cursor.fetchone()
        self._stored_count = int(row[0]) if row else 0
        # Warm the cache with the most recent rows rather than every row: the
        # database itself is the source of truth for duplicates.
        self._seen.clear()
        if self.cache_limit:
            async with self._db.execute(
                "SELECT fingerprint FROM domains ORDER BY first_seen DESC LIMIT ?",
                (self.cache_limit,),
            ) as cursor:
                async for row in cursor:
                    self._seen[row[0]] = None
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
            ("site", "ALTER TABLE domains ADD COLUMN site TEXT"),
            ("is_primary", "ALTER TABLE domains ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 1"),
        ):
            if column not in existing:
                await self._db.execute(ddl)
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS frontier (
                fingerprint TEXT PRIMARY KEY,
                origin      TEXT,
                added       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
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
        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_domains_responsive ON domains(responsive)",
            "CREATE INDEX IF NOT EXISTS idx_domains_source ON domains(source)",
            "CREATE INDEX IF NOT EXISTS idx_domains_first_seen ON domains(first_seen, fingerprint)",
            "CREATE INDEX IF NOT EXISTS idx_domains_checked_at ON domains(checked_at)",
            "CREATE INDEX IF NOT EXISTS idx_domain_tech_tech ON domain_tech(technology)",
            "CREATE INDEX IF NOT EXISTS idx_domains_site ON domains(site)",
            "CREATE INDEX IF NOT EXISTS idx_domains_primary "
            "ON domains(is_primary, first_seen, fingerprint)",
        ):
            await self._db.execute(statement)
        await self._backfill_sites()
        # Rows written before first_seen had a default would otherwise need a
        # slower NULL-aware ordering on every read.
        await self._db.execute(
            "UPDATE domains SET first_seen = COALESCE(checked_at, CURRENT_TIMESTAMP) "
            "WHERE first_seen IS NULL"
        )
        await self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        await self._db.commit()

    async def _backfill_sites(self) -> None:
        """Fill in the site of rows written before the column existed."""
        assert self._db is not None
        while True:
            async with self._db.execute(
                "SELECT fingerprint, raw FROM domains WHERE site IS NULL LIMIT 20000"
            ) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                break
            await self._db.executemany(
                "UPDATE domains SET site = ? WHERE fingerprint = ?",
                [(site_of(raw), fingerprint) for fingerprint, raw in rows],
            )
            await self._db.commit()
            await self._reconcile_sites({site_of(raw) for _fingerprint, raw in rows})

    async def _reconcile_sites(self, sites: Set[str]) -> None:
        """Elect one representative host per site.

        The listing shows a site once rather than once per host name, so
        ``example.com`` and ``www.example.com`` do not read as two results.
        The winner is the shortest name, which is always the apex; ordering
        by name alone would pick whichever variant sorted first.
        """
        assert self._db is not None
        names = [site for site in sites if site]
        if not names:
            return
        for start in range(0, len(names), 400):
            chunk = names[start:start + 400]
            placeholders = ",".join("?" * len(chunk))
            await self._db.execute(
                f"UPDATE domains SET is_primary = 0 "
                f"WHERE site IN ({placeholders}) AND is_primary = 1",
                chunk,
            )
            await self._db.execute(
                f"""
                UPDATE domains SET is_primary = 1 WHERE fingerprint IN (
                    SELECT fingerprint FROM (
                        SELECT fingerprint, ROW_NUMBER() OVER (
                            PARTITION BY site
                            ORDER BY length(raw) - length(replace(raw, '.', '')),
                                     length(raw), raw
                        ) AS rank FROM domains WHERE site IN ({placeholders})
                    ) WHERE rank = 1
                )
                """,
                chunk,
            )

    # ------------------------------------------------------------ dedup logic
    def _remember(self, fingerprint: str) -> None:
        if not self.cache_limit:
            return
        self._seen[fingerprint] = None
        self._seen.move_to_end(fingerprint)
        while len(self._seen) > self.cache_limit:
            self._seen.popitem(last=False)

    def is_known(self, fingerprint: str) -> bool:
        """``True`` if the domain is cached as stored or already queued.

        Only consults memory. Use :meth:`filter_new` to also rule out domains
        that are in the database but no longer cached.
        """
        return fingerprint in self._seen or fingerprint in self._reserved

    async def filter_new(self, fingerprints: Sequence[str]) -> List[str]:
        """Return the fingerprints that are neither queued nor already stored.

        Candidates missing from the in-memory cache are checked against the
        database in one statement, which keeps duplicate rejection exact
        without holding every known fingerprint in RAM.
        """
        candidates: List[str] = []
        seen_here: Set[str] = set()
        for fingerprint in fingerprints:
            if fingerprint in seen_here or self.is_known(fingerprint):
                continue
            seen_here.add(fingerprint)
            candidates.append(fingerprint)
        if not candidates or self._db is None:
            return candidates

        await self.flush()
        known: Set[str] = set()
        chunk_size = 400
        for start in range(0, len(candidates), chunk_size):
            chunk = candidates[start:start + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            async with self._db.execute(
                f"SELECT fingerprint FROM domains WHERE fingerprint IN ({placeholders})",
                chunk,
            ) as cursor:
                async for row in cursor:
                    known.add(row[0])
        for fingerprint in known:
            self._remember(fingerprint)
        return [fingerprint for fingerprint in candidates if fingerprint not in known]

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
        """Domains stored in the database, including those not cached."""
        return self._stored_count

    # ---------------------------------------------------------------- writing
    async def add(self, record: DomainRecord) -> bool:
        """Buffer *record* for writing.  ``False`` when it is a duplicate.

        A record flagged ``recheck`` updates the existing row instead of being
        rejected as a duplicate.
        """
        if record.recheck:
            self._reserved.discard(record.fingerprint)
            self._pending.append(record)
            if len(self._pending) >= self.flush_size:
                await self.flush()
            return True
        if record.fingerprint in self._seen:
            self._reserved.discard(record.fingerprint)
            return False
        self._remember(record.fingerprint)
        self._stored_count += 1
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
            if self._db is None:
                # Keep the buffer intact; taking it here would drop the records.
                return 0
            batch, self._pending = self._pending, []
            # UTC, to match SQLite's CURRENT_TIMESTAMP default on first_seen
            # and the cutoff used by stale_domains().
            now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

            def values(record: DomainRecord):
                return (
                    record.fingerprint,
                    record.raw,
                    site_of(record.raw),
                    1 if record.responsive else 0,
                    json.dumps(record.technologies) if record.technologies else None,
                    record.status_code,
                    record.scheme,
                    record.error,
                    record.elapsed_ms,
                    record.source,
                    now if record.probed else None,
                )

            rows = [values(record) for record in batch if not record.recheck]
            updates = [values(record)[3:] + (record.fingerprint,) for record in batch if record.recheck]
            new_sites = {row[2] for row in rows}
            rechecked = [record.fingerprint for record in batch if record.recheck]
            tech_rows = [
                (record.fingerprint, technology, record.versions.get(technology) or None)
                for record in batch
                for technology in record.technologies
            ]
            previous_pairs: Set[Tuple[str, str]] = set()
            if rechecked and self.write_tech_files:
                placeholders = ",".join("?" * len(rechecked))
                async with self._db.execute(
                    f"SELECT fingerprint, technology FROM domain_tech WHERE fingerprint IN ({placeholders})",
                    rechecked,
                ) as cursor:
                    previous_pairs = {(row[0], row[1]) async for row in cursor}
            try:
                if rows:
                    await self._db.executemany(
                        """
                        INSERT OR IGNORE INTO domains
                            (fingerprint, raw, site, responsive, technologies, status_code,
                             scheme, error, elapsed_ms, source, checked_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                    await self._reconcile_sites(new_sites)
                if updates:
                    await self._db.executemany(
                        """
                        UPDATE domains SET responsive = ?, technologies = ?, status_code = ?,
                               scheme = ?, error = ?, elapsed_ms = ?, source = ?, checked_at = ?
                        WHERE fingerprint = ?
                        """,
                        updates,
                    )
                    # Technologies can disappear between checks - replace, do not merge.
                    await self._db.executemany(
                        "DELETE FROM domain_tech WHERE fingerprint = ?",
                        [(fingerprint,) for fingerprint in rechecked],
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
                await self._write_tech_files(batch, previous_pairs)
            return len(batch)

    async def _write_tech_files(
        self, batch: Sequence[DomainRecord], already_written: Optional[Set[Tuple[str, str]]] = None
    ) -> None:
        already_written = already_written or set()
        grouped: Dict[str, List[str]] = {}
        for record in batch:
            if not record.responsive:
                continue
            for technology in record.technologies:
                if (record.fingerprint, technology) in already_written:
                    continue  # a re-check that found the same technology again
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

    # --------------------------------------------------------------- frontier
    async def push_frontier(self, fingerprints: Sequence[str], origin: str = "") -> int:
        """Queue self-discovered candidates for a later cycle.

        Anything already stored is dropped, so the frontier only ever holds
        work that still needs doing.
        """
        if self._db is None or not fingerprints:
            return 0
        unique = [fp for fp in dict.fromkeys(fingerprints) if fp and not self.is_known(fp)]
        if not unique:
            return 0
        if len(unique) > self.frontier_limit:
            unique = unique[: self.frontier_limit]
        try:
            await self._db.executemany(
                "INSERT OR IGNORE INTO frontier (fingerprint, origin) "
                "SELECT ?, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM domains WHERE fingerprint = ?)",
                [(fp, origin, fp) for fp in unique],
            )
            await self._db.commit()
        except Exception:
            return 0
        return len(unique)

    async def take_frontier(self, limit: int) -> List[str]:
        """Remove and return up to *limit* queued candidates."""
        if self._db is None or limit <= 0:
            return []
        async with self._db.execute(
            "SELECT fingerprint FROM frontier ORDER BY added ASC LIMIT ?", (int(limit),)
        ) as cursor:
            rows = [row[0] async for row in cursor]
        if not rows:
            return []
        placeholders = ",".join("?" * len(rows))
        await self._db.execute(
            f"DELETE FROM frontier WHERE fingerprint IN ({placeholders})", rows
        )
        await self._db.commit()
        return rows

    async def frontier_size(self) -> int:
        if self._db is None:
            return 0
        async with self._db.execute("SELECT COUNT(*) FROM frontier") as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def trim_frontier(self) -> int:
        """Drop the oldest entries once the frontier exceeds its limit."""
        if self._db is None:
            return 0
        size = await self.frontier_size()
        excess = size - self.frontier_limit
        if excess <= 0:
            return 0
        await self._db.execute(
            "DELETE FROM frontier WHERE fingerprint IN "
            "(SELECT fingerprint FROM frontier ORDER BY added ASC LIMIT ?)",
            (excess,),
        )
        await self._db.commit()
        return excess

    # ---------------------------------------------------------------- queries
    async def stale_domains(self, older_than_seconds: int, limit: int = 100) -> List[str]:
        """Fingerprints last checked more than *older_than_seconds* ago.

        Rows written by the 1.x collector have no ``checked_at`` at all; they
        fall back to ``first_seen``, and are re-checked first.
        """
        if older_than_seconds <= 0 or limit <= 0:
            return []
        await self.flush()
        assert self._db is not None
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - older_than_seconds))
        async with self._db.execute(
            """
            SELECT fingerprint FROM domains
            WHERE COALESCE(checked_at, first_seen, '') < ?
            ORDER BY COALESCE(checked_at, first_seen, '') ASC
            LIMIT ?
            """,
            (cutoff, int(limit)),
        ) as cursor:
            return [row[0] async for row in cursor]

    async def last_checked(self, fingerprint: str) -> Optional[str]:
        """``checked_at`` for one domain (used by tests and the CLI)."""
        await self.flush()
        assert self._db is not None
        async with self._db.execute(
            "SELECT checked_at FROM domains WHERE fingerprint = ?", (fingerprint,)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

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
