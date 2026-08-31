"""Persistence: SQLite database plus per-technology text files.

A single long-lived connection runs in WAL mode with a busy timeout, so readers
never block the writer. Writes are batched by a background task and flushed
with ``executemany``. ``reserve()`` claims a fingerprint before it is queued so
no domain is probed twice.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import aiosqlite

from .domains import site_of, site_rank

SCHEMA_VERSION = 4

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
        self._tech_ids_cache: Dict[str, int] = {}
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
                first_seen  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                responsive  INTEGER NOT NULL DEFAULT 0
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
        # Technology names are interned. Storing the domain name and the
        # technology name in full on every pairing made this table and its
        # indexes half the database; two integers per pairing is a fraction of
        # that, and the counts a listing needs are maintained rather than
        # recomputed by scanning the whole table.
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS technologies (
                id   INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS tech_counts (
                tech_id INTEGER PRIMARY KEY,
                domains INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Totals for the dashboard. Counting rows instead is a full scan, which
        # is milliseconds at a million domains and many seconds at a hundred
        # million, on a panel that refreshes while a run is going.
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS totals (
                name  TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS domain_tech (
                domain_id INTEGER NOT NULL,
                tech_id   INTEGER NOT NULL,
                version   TEXT,
                PRIMARY KEY (domain_id, tech_id)
            ) WITHOUT ROWID
            """
        )
        await self._upgrade_domain_tech()
        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_domains_responsive ON domains(responsive)",
            "CREATE INDEX IF NOT EXISTS idx_domains_source ON domains(source)",
            "CREATE INDEX IF NOT EXISTS idx_domains_first_seen ON domains(first_seen, fingerprint)",
            "CREATE INDEX IF NOT EXISTS idx_domains_checked_at ON domains(checked_at)",
            "CREATE INDEX IF NOT EXISTS idx_domain_tech_tech ON domain_tech(tech_id, domain_id)",
            "CREATE INDEX IF NOT EXISTS idx_domains_site ON domains(site)",
            "CREATE INDEX IF NOT EXISTS idx_domains_primary "
            "ON domains(is_primary, first_seen, fingerprint)",
        ):
            await self._db.execute(statement)
        await self._drop_duplicated_columns()
        await self._backfill_sites()
        await self._recount_totals_if_missing()
        # Rows written before first_seen had a default would otherwise need a
        # slower NULL-aware ordering on every read.
        await self._db.execute(
            "UPDATE domains SET first_seen = COALESCE(checked_at, CURRENT_TIMESTAMP) "
            "WHERE first_seen IS NULL"
        )
        await self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        await self._db.commit()

    async def _upgrade_domain_tech(self) -> None:
        """Convert a name-keyed domain_tech table to the interned one.

        Runs once, on a database written before technology names were interned.
        The old table is read through the domains table so each pairing picks
        up the integer row id SQLite already keeps for its domain.
        """
        assert self._db is not None
        columns = set()
        async with self._db.execute("PRAGMA table_info(domain_tech)") as cursor:
            async for row in cursor:
                columns.add(row[1])
        if "fingerprint" not in columns:
            await self._refresh_tech_counts_if_empty()
            return

        await self._db.execute("DROP INDEX IF EXISTS idx_domain_tech_tech")
        await self._db.execute("ALTER TABLE domain_tech RENAME TO domain_tech_legacy")
        await self._db.execute(
            """
            CREATE TABLE domain_tech (
                domain_id INTEGER NOT NULL,
                tech_id   INTEGER NOT NULL,
                version   TEXT,
                PRIMARY KEY (domain_id, tech_id)
            ) WITHOUT ROWID
            """
        )
        await self._db.execute(
            "INSERT OR IGNORE INTO technologies (name) "
            "SELECT DISTINCT technology FROM domain_tech_legacy"
        )
        await self._db.execute(
            """
            INSERT OR IGNORE INTO domain_tech (domain_id, tech_id, version)
            SELECT d.rowid, t.id, l.version
            FROM domain_tech_legacy l
            JOIN domains d ON d.fingerprint = l.fingerprint
            JOIN technologies t ON t.name = l.technology
            """
        )
        await self._db.execute("DROP TABLE domain_tech_legacy")
        await self._recount_technologies()
        await self._db.commit()

    TOTALS = ("domains", "responsive", "onion")

    async def _recount_totals_if_missing(self) -> None:
        """Seed the totals table, once, from the rows it summarises."""
        assert self._db is not None
        async with self._db.execute("SELECT COUNT(*) FROM totals") as cursor:
            row = await cursor.fetchone()
        if row and int(row[0]) == len(self.TOTALS):
            return
        await self._recount_totals()

    async def _recount_totals(self) -> None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(responsive), 0), "
            "       COALESCE(SUM(fingerprint LIKE '%.onion'), 0) FROM domains"
        ) as cursor:
            row = await cursor.fetchone()
        values = [int(value) for value in (row or (0, 0, 0))]
        await self._db.executemany(
            "INSERT INTO totals (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            list(zip(self.TOTALS, values)),
        )
        await self._db.commit()

    async def _bump_totals(self, deltas: Dict[str, int]) -> None:
        assert self._db is not None
        changed = [(name, delta) for name, delta in deltas.items() if delta]
        if not changed:
            return
        await self._db.executemany(
            "INSERT INTO totals (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = MAX(0, value + excluded.value)",
            changed,
        )

    async def _refresh_tech_counts_if_empty(self) -> None:
        """Populate the counter table the first time it appears."""
        assert self._db is not None
        async with self._db.execute("SELECT 1 FROM tech_counts LIMIT 1") as cursor:
            if await cursor.fetchone():
                return
        async with self._db.execute("SELECT 1 FROM domain_tech LIMIT 1") as cursor:
            if not await cursor.fetchone():
                return
        await self._recount_technologies()
        await self._db.commit()

    async def _recount_technologies(self) -> None:
        """Rebuild the counter table from the pairings it summarises."""
        assert self._db is not None
        await self._db.execute("DELETE FROM tech_counts")
        await self._db.execute(
            "INSERT INTO tech_counts (tech_id, domains) "
            "SELECT tech_id, COUNT(*) FROM domain_tech GROUP BY tech_id"
        )

    async def _drop_duplicated_columns(self) -> None:
        """Remove the two columns that repeated data held elsewhere.

        ``raw`` always held the same string as ``fingerprint``, and
        ``technologies`` held a JSON copy of the domain_tech pairings. Between
        them they were a fifth of the file. The table is rebuilt once; the
        pairings key on fingerprint, so reassigned row ids do not matter as
        long as domain_tech is remapped afterwards.
        """
        assert self._db is not None
        columns = set()
        async with self._db.execute("PRAGMA table_info(domains)") as cursor:
            async for row in cursor:
                columns.add(row[1])
        if not ({"raw", "technologies"} & columns):
            return
        await self._db.execute("PRAGMA foreign_keys=OFF")
        await self._db.execute(
            """
            CREATE TABLE domains_rebuilt (
                fingerprint TEXT PRIMARY KEY,
                first_seen  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                responsive  INTEGER NOT NULL DEFAULT 0,
                status_code INTEGER,
                scheme      TEXT,
                error       TEXT,
                elapsed_ms  INTEGER,
                source      TEXT,
                checked_at  TIMESTAMP,
                site        TEXT,
                is_primary  INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        await self._db.execute(
            """
            INSERT INTO domains_rebuilt
                (fingerprint, first_seen, responsive, status_code, scheme, error,
                 elapsed_ms, source, checked_at, site, is_primary)
            SELECT fingerprint, first_seen, responsive, status_code, scheme, error,
                   elapsed_ms, source, checked_at, site, is_primary FROM domains
            """
        )
        # domain_tech points at row ids the rebuild reassigns, so carry the
        # pairings across by name before the old table goes away.
        await self._db.execute(
            """
            CREATE TEMPORARY TABLE tech_by_name AS
            SELECT d.fingerprint AS fingerprint, dt.tech_id AS tech_id, dt.version AS version
            FROM domain_tech dt JOIN domains d ON d.rowid = dt.domain_id
            """
        )
        await self._db.execute("DROP TABLE domains")
        await self._db.execute("ALTER TABLE domains_rebuilt RENAME TO domains")
        await self._db.execute("DELETE FROM domain_tech")
        await self._db.execute(
            """
            INSERT OR IGNORE INTO domain_tech (domain_id, tech_id, version)
            SELECT d.rowid, n.tech_id, n.version
            FROM tech_by_name n JOIN domains d ON d.fingerprint = n.fingerprint
            """
        )
        await self._db.execute("DROP TABLE tech_by_name")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.commit()

    async def _backfill_sites(self) -> None:
        """Fill in the site of rows written before the column existed."""
        assert self._db is not None
        while True:
            async with self._db.execute(
                "SELECT fingerprint FROM domains WHERE site IS NULL LIMIT 20000"
            ) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                break
            await self._db.executemany(
                "UPDATE domains SET site = ? WHERE fingerprint = ?",
                [(site_of(name), name) for (name,) in rows],
            )
            await self._db.commit()
            await self._reconcile_sites({site_of(name) for (name,) in rows})

    async def _reconcile_sites(self, sites: Set[str]) -> None:
        """Elect one representative host per site.

        The listing shows a site once rather than once per host name, so
        ``example.com`` and ``www.example.com`` do not read as two results.
        The winner is the shortest name, which is always the apex; ordering by
        name alone would pick whichever variant sorted first, and the choice
        does not drift as more variants arrive.

        Only the sites this batch touched are read, and only the rows whose
        flag is actually wrong are written, so a bulk load does not rewrite
        rows it has already settled.
        """
        assert self._db is not None
        names = [site for site in sites if site]
        if not names:
            return
        for start in range(0, len(names), 400):
            chunk = names[start:start + 400]
            placeholders = ",".join("?" * len(chunk))
            members: Dict[str, List[Tuple[str, int]]] = {}
            async with self._db.execute(
                f"SELECT site, fingerprint, is_primary FROM domains "
                f"WHERE site IN ({placeholders})",
                chunk,
            ) as cursor:
                async for row in cursor:
                    members.setdefault(row[0], []).append((row[1], int(row[2])))
            promote, demote = [], []
            for hosts in members.values():
                best = min(hosts, key=lambda entry: site_rank(entry[0]))[0]
                for host, is_primary in hosts:
                    if host == best and not is_primary:
                        promote.append(host)
                    elif host != best and is_primary:
                        demote.append(host)
            for value, changed in ((0, demote), (1, promote)):
                for offset in range(0, len(changed), 400):
                    piece = changed[offset:offset + 400]
                    await self._db.execute(
                        f"UPDATE domains SET is_primary = {value} "
                        f"WHERE fingerprint IN ({','.join('?' * len(piece))})",
                        piece,
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
                    site_of(record.fingerprint),
                    1 if record.responsive else 0,
                    record.status_code,
                    record.scheme,
                    record.error,
                    record.elapsed_ms,
                    record.source,
                    now if record.probed else None,
                )

            rows = [values(record) for record in batch if not record.recheck]
            updates = [values(record)[2:] + (record.fingerprint,) for record in batch if record.recheck]
            new_sites = {row[1] for row in rows}
            rechecked = [record.fingerprint for record in batch if record.recheck]
            previous_pairs: Set[Tuple[str, str]] = set()
            if rechecked and self.write_tech_files:
                previous_pairs = await self._pairs_for(rechecked)
            # A re-check can flip a domain between live and down, so the
            # running totals need the value it is replacing.
            was_responsive = 0
            if rechecked:
                for start in range(0, len(rechecked), 400):
                    chunk = rechecked[start:start + 400]
                    placeholders = ",".join("?" * len(chunk))
                    async with self._db.execute(
                        f"SELECT COALESCE(SUM(responsive), 0) FROM domains "
                        f"WHERE fingerprint IN ({placeholders})",
                        chunk,
                    ) as cursor:
                        row = await cursor.fetchone()
                    was_responsive += int(row[0]) if row else 0
            try:
                before_insert = self._db.total_changes
                inserted = 0
                if rows:
                    await self._db.executemany(
                        """
                        INSERT OR IGNORE INTO domains
                            (fingerprint, site, responsive, status_code,
                             scheme, error, elapsed_ms, source, checked_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                    inserted = self._db.total_changes - before_insert
                    await self._reconcile_sites(new_sites)
                if updates:
                    await self._db.executemany(
                        """
                        UPDATE domains SET responsive = ?, status_code = ?,
                               scheme = ?, error = ?, elapsed_ms = ?, source = ?, checked_at = ?
                        WHERE fingerprint = ?
                        """,
                        updates,
                    )
                    # Technologies can disappear between checks - replace, do not merge.
                    await self._clear_technologies(rechecked)
                await self._write_technologies(batch)
                # INSERT OR IGNORE can skip a row another writer got to first,
                # so the totals follow what the database actually accepted.
                if inserted:
                    accepted = rows[:inserted] if inserted < len(rows) else rows
                    await self._bump_totals({
                        "domains": inserted,
                        "responsive": sum(row[2] for row in accepted),
                        "onion": sum(1 for row in accepted if row[0].endswith(".onion")),
                    })
                if updates:
                    await self._bump_totals({
                        "responsive": sum(update[0] for update in updates) - was_responsive,
                    })
                await self._db.commit()
            except Exception:
                # Put the batch back so nothing is silently lost.
                self._pending = batch + self._pending
                raise
            if self.write_tech_files:
                await self._write_tech_files(batch, previous_pairs)
            return len(batch)

    async def _tech_ids(self, names: Sequence[str]) -> Dict[str, int]:
        """Intern technology names, returning their ids."""
        assert self._db is not None
        wanted = {name for name in names if name}
        if not wanted:
            return {}
        missing = [name for name in wanted if name not in self._tech_ids_cache]
        if missing:
            await self._db.executemany(
                "INSERT OR IGNORE INTO technologies (name) VALUES (?)",
                [(name,) for name in missing],
            )
            for start in range(0, len(missing), 400):
                chunk = missing[start:start + 400]
                placeholders = ",".join("?" * len(chunk))
                async with self._db.execute(
                    f"SELECT name, id FROM technologies WHERE name IN ({placeholders})", chunk
                ) as cursor:
                    async for row in cursor:
                        self._tech_ids_cache[row[0]] = int(row[1])
        return {name: self._tech_ids_cache[name] for name in wanted
                if name in self._tech_ids_cache}

    async def _domain_ids(self, fingerprints: Sequence[str]) -> Dict[str, int]:
        """Row ids for stored domains, looked up a chunk at a time."""
        assert self._db is not None
        found: Dict[str, int] = {}
        names = list({name for name in fingerprints if name})
        for start in range(0, len(names), 400):
            chunk = names[start:start + 400]
            placeholders = ",".join("?" * len(chunk))
            async with self._db.execute(
                f"SELECT fingerprint, rowid FROM domains WHERE fingerprint IN ({placeholders})",
                chunk,
            ) as cursor:
                async for row in cursor:
                    found[row[0]] = int(row[1])
        return found

    async def _pairs_for(self, fingerprints: Sequence[str]) -> Set[Tuple[str, str]]:
        """``(domain, technology)`` pairings currently stored for *fingerprints*."""
        assert self._db is not None
        ids = await self._domain_ids(fingerprints)
        if not ids:
            return set()
        by_id = {value: key for key, value in ids.items()}
        pairs: Set[Tuple[str, str]] = set()
        values = list(by_id)
        for start in range(0, len(values), 400):
            chunk = values[start:start + 400]
            placeholders = ",".join("?" * len(chunk))
            async with self._db.execute(
                f"SELECT dt.domain_id, t.name FROM domain_tech dt "
                f"JOIN technologies t ON t.id = dt.tech_id "
                f"WHERE dt.domain_id IN ({placeholders})",
                chunk,
            ) as cursor:
                async for row in cursor:
                    pairs.add((by_id[int(row[0])], row[1]))
        return pairs

    async def _clear_technologies(self, fingerprints: Sequence[str]) -> None:
        """Drop the pairings for *fingerprints*, keeping the counters in step."""
        assert self._db is not None
        ids = await self._domain_ids(fingerprints)
        if not ids:
            return
        values = list(ids.values())
        for start in range(0, len(values), 400):
            chunk = values[start:start + 400]
            placeholders = ",".join("?" * len(chunk))
            await self._db.execute(
                f"UPDATE tech_counts SET domains = MAX(0, domains - ("
                f"  SELECT COUNT(*) FROM domain_tech dt"
                f"  WHERE dt.tech_id = tech_counts.tech_id"
                f"    AND dt.domain_id IN ({placeholders})))"
                f"WHERE tech_id IN ("
                f"  SELECT tech_id FROM domain_tech WHERE domain_id IN ({placeholders}))",
                chunk + chunk,
            )
            await self._db.execute(
                f"DELETE FROM domain_tech WHERE domain_id IN ({placeholders})", chunk
            )

    async def _write_technologies(self, batch: Sequence[DomainRecord]) -> None:
        """Store this batch's technology pairings and bump the counters.

        The counts shown on the Technologies page are maintained here rather
        than recomputed: counting the pairings is a full scan of the largest
        table, which is a fraction of a second at a million domains and many
        seconds at a hundred million.
        """
        assert self._db is not None
        wanted = [record for record in batch if record.technologies]
        if not wanted:
            return
        tech_ids = await self._tech_ids(
            [name for record in wanted for name in record.technologies]
        )
        domain_ids = await self._domain_ids([record.fingerprint for record in wanted])
        pairs = []
        for record in wanted:
            domain_id = domain_ids.get(record.fingerprint)
            if domain_id is None:
                continue
            for name in record.technologies:
                tech_id = tech_ids.get(name)
                if tech_id is not None:
                    pairs.append((domain_id, tech_id, record.versions.get(name) or None))
        if not pairs:
            return
        # Only pairings that are actually new may increment a counter.
        existing = set()
        for start in range(0, len(pairs), 400):
            chunk = pairs[start:start + 400]
            placeholders = ",".join("(?,?)" for _ in chunk)
            flat: List[Any] = []
            for domain_id, tech_id, _version in chunk:
                flat.extend((domain_id, tech_id))
            async with self._db.execute(
                f"SELECT domain_id, tech_id FROM domain_tech "
                f"WHERE (domain_id, tech_id) IN ({placeholders})",
                flat,
            ) as cursor:
                async for row in cursor:
                    existing.add((int(row[0]), int(row[1])))
        await self._db.executemany(
            "INSERT OR IGNORE INTO domain_tech (domain_id, tech_id, version) VALUES (?, ?, ?)",
            pairs,
        )
        added: Dict[int, int] = {}
        for domain_id, tech_id, _version in pairs:
            if (domain_id, tech_id) not in existing:
                added[tech_id] = added.get(tech_id, 0) + 1
        if added:
            await self._db.executemany(
                "INSERT INTO tech_counts (tech_id, domains) VALUES (?, ?) "
                "ON CONFLICT(tech_id) DO UPDATE SET domains = domains + excluded.domains",
                list(added.items()),
            )

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
                grouped.setdefault(safe_filename(technology), []).append(record.fingerprint)
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
        totals = {name: 0 for name in self.TOTALS}
        async with self._db.execute("SELECT name, value FROM totals") as cursor:
            async for row in cursor:
                totals[row[0]] = int(row[1])
        async with self._db.execute(
            "SELECT COUNT(*) FROM tech_counts WHERE domains > 0"
        ) as cursor:
            tech_row = await cursor.fetchone()
        return {
            "total": totals["domains"],
            "responsive": totals["responsive"],
            "technologies": int(tech_row[0] if tech_row else 0),
        }

    async def top_technologies(self, limit: int = 20) -> List[Tuple[str, int]]:
        await self.flush()
        assert self._db is not None
        async with self._db.execute(
            "SELECT t.name, c.domains FROM tech_counts c "
            "JOIN technologies t ON t.id = c.tech_id "
            "WHERE c.domains > 0 ORDER BY c.domains DESC, t.name ASC LIMIT ?",
            (int(limit),),
        ) as cursor:
            return [(row[0], int(row[1])) async for row in cursor]

    async def domains_for_technology(self, technology: str, limit: int = 1000) -> List[str]:
        await self.flush()
        assert self._db is not None
        async with self._db.execute(
            "SELECT d.fingerprint FROM domain_tech dt "
            "JOIN domains d ON d.rowid = dt.domain_id "
            "JOIN technologies t ON t.id = dt.tech_id "
            "WHERE t.name = ? ORDER BY d.fingerprint LIMIT ?",
            (technology, int(limit)),
        ) as cursor:
            return [row[0] async for row in cursor]
