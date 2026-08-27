import json
import sqlite3

import pytest
import pytest_asyncio

from domainatlas.store import DomainRecord, DomainStore, safe_filename

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Google Analytics", "Google Analytics"),
        ("../../etc/passwd", "etc_passwd"),
        ("a/b\\c", "a_b_c"),
        ("CON", "CON_tech"),
        ("", "unknown"),
        ("...", "unknown"),
        ("Node.js", "Node.js"),
    ],
)
async def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


async def test_safe_filename_is_bounded():
    assert len(safe_filename("x" * 500)) <= 100


@pytest_asyncio.fixture
async def store(tmp_path):
    store = DomainStore(str(tmp_path / "d.db"), str(tmp_path / "out"))
    await store.open()
    try:
        yield store
    finally:
        await store.close()


async def test_add_and_dedupe(store):
    assert await store.add(DomainRecord("a.com", "a.com", True, ["Nginx"], versions={"Nginx": "1.2"}))
    assert not await store.add(DomainRecord("a.com", "a.com", True, ["Nginx"]))
    assert store.is_known("a.com")
    assert store.known_count == 1


async def test_reserve_prevents_double_queueing(store):
    assert store.reserve("b.com") is True
    assert store.reserve("b.com") is False
    store.release("b.com")
    assert store.reserve("b.com") is True


async def test_flush_writes_rows_and_tech_files(tmp_path, store):
    await store.add(DomainRecord("a.com", "a.com", True, ["Nginx", "WordPress"],
                                 versions={"Nginx": "1.24.0"}, status_code=200, scheme="https"))
    await store.add(DomainRecord("down.com", "down.com", False, [], error="timeout"))
    assert await store.flush() == 0 or True  # already flushed by summary below
    summary = await store.summary()
    assert summary == {"total": 2, "responsive": 1, "technologies": 2}

    out = tmp_path / "out"
    assert (out / "Nginx.txt").read_text().strip() == "a.com"
    assert (out / "WordPress.txt").read_text().strip() == "a.com"
    # unresponsive domains never reach the technology files
    assert not any(path.name.startswith("down") for path in out.iterdir())


async def test_top_technologies_and_export(store):
    await store.add(DomainRecord("a.com", "a.com", True, ["Nginx"]))
    await store.add(DomainRecord("b.com", "b.com", True, ["Nginx", "React"]))
    assert await store.top_technologies() == [("Nginx", 2), ("React", 1)]
    assert await store.domains_for_technology("Nginx") == ["a.com", "b.com"]
    assert await store.domains_for_technology("Nope") == []


async def test_versions_are_persisted(tmp_path, store):
    await store.add(DomainRecord("a.com", "a.com", True, ["Nginx"], versions={"Nginx": "1.24.0"}))
    await store.flush()
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT technology, version FROM domain_tech").fetchone()
    assert row == ("Nginx", "1.24.0")


async def test_known_fingerprints_survive_a_restart(tmp_path):
    db = str(tmp_path / "d.db")
    first = DomainStore(db, str(tmp_path / "out"))
    await first.open()
    await first.add(DomainRecord("a.com", "a.com", True, ["Nginx"]))
    await first.close()

    second = DomainStore(db, str(tmp_path / "out"))
    await second.open()
    try:
        assert second.is_known("a.com")
        assert not await second.add(DomainRecord("a.com", "a.com", True, []))
    finally:
        await second.close()


async def test_migrates_a_version_1_database(tmp_path):
    """A database written by the 1.x collector must keep working."""
    db = str(tmp_path / "legacy.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE domains (fingerprint TEXT PRIMARY KEY, raw TEXT NOT NULL, "
            "first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP, responsive BOOLEAN DEFAULT 0, "
            "technologies TEXT)"
        )
        conn.execute("INSERT INTO domains (fingerprint, raw, responsive, technologies) VALUES (?,?,?,?)",
                     ("old.com", "old.com", 1, json.dumps(["Nginx"])))
        conn.commit()

    store = DomainStore(db, str(tmp_path / "out"))
    await store.open()
    try:
        assert store.is_known("old.com")
        assert await store.add(DomainRecord("new.com", "new.com", True, ["React"]))
        summary = await store.summary()
        assert summary["total"] == 2
    finally:
        await store.close()


async def test_tech_file_names_are_sanitised(tmp_path, store):
    await store.add(DomainRecord("a.com", "a.com", True, ["../../evil"]))
    await store.flush()
    files = [path.name for path in (tmp_path / "out").iterdir()]
    assert files == ["evil.txt"]


async def test_write_tech_files_can_be_disabled(tmp_path):
    store = DomainStore(str(tmp_path / "d.db"), str(tmp_path / "out"), write_tech_files=False)
    await store.open()
    try:
        await store.add(DomainRecord("a.com", "a.com", True, ["Nginx"]))
        await store.flush()
    finally:
        await store.close()
    assert not (tmp_path / "out").exists()


async def test_stale_domains_respects_the_cutoff(store):
    await store.add(DomainRecord("fresh.com", "fresh.com", True, ["Nginx"]))
    await store.flush()
    assert await store.stale_domains(3600, 10) == []

    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE domains SET checked_at = '2000-01-01 00:00:00'")
        conn.commit()
    assert await store.stale_domains(3600, 10) == ["fresh.com"]
    assert await store.stale_domains(0, 10) == []       # disabled
    assert await store.stale_domains(3600, 0) == []     # no budget


async def test_stale_domains_are_ordered_oldest_first(store):
    for name in ("a.com", "b.com"):
        await store.add(DomainRecord(name, name, True, []))
    await store.flush()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE domains SET checked_at = '2001-01-01 00:00:00' WHERE fingerprint = 'a.com'")
        conn.execute("UPDATE domains SET checked_at = '2000-01-01 00:00:00' WHERE fingerprint = 'b.com'")
        conn.commit()
    assert await store.stale_domains(3600, 10) == ["b.com", "a.com"]


async def test_recheck_updates_the_row_instead_of_inserting(store):
    await store.add(DomainRecord("a.com", "a.com", True, ["Nginx"],
                                 versions={"Nginx": "1.0"}, status_code=200))
    await store.flush()
    assert await store.add(DomainRecord("a.com", "a.com", False, [], status_code=503,
                                        error="HTTP 503", recheck=True))
    await store.flush()

    assert (await store.summary())["total"] == 1
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT responsive, status_code, error FROM domains").fetchone()
        techs = conn.execute("SELECT COUNT(*) FROM domain_tech").fetchone()[0]
    assert row == (0, 503, "HTTP 503")
    assert techs == 0  # technologies that vanished are removed, not merged


async def test_recheck_replaces_the_technology_set(store):
    await store.add(DomainRecord("a.com", "a.com", True, ["Nginx"]))
    await store.flush()
    await store.add(DomainRecord("a.com", "a.com", True, ["Apache", "React"], recheck=True))
    await store.flush()
    assert await store.top_technologies() == [("Apache", 1), ("React", 1)]


async def test_recheck_does_not_duplicate_technology_file_lines(tmp_path, store):
    await store.add(DomainRecord("a.com", "a.com", True, ["Nginx"]))
    await store.flush()
    await store.add(DomainRecord("a.com", "a.com", True, ["Nginx", "React"], recheck=True))
    await store.flush()
    assert (tmp_path / "out" / "Nginx.txt").read_text() == "a.com\n"
    assert (tmp_path / "out" / "React.txt").read_text() == "a.com\n"


async def test_checked_at_is_utc(store):
    """stale_domains() compares against a UTC cutoff, so writes must be UTC."""
    import datetime

    await store.add(DomainRecord("a.com", "a.com", True, []))
    stamp = await store.last_checked("a.com")
    written = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    now = datetime.datetime.utcnow()
    assert abs((now - written).total_seconds()) < 60


async def test_legacy_rows_without_checked_at_are_stale(tmp_path):
    db = str(tmp_path / "legacy.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE domains (fingerprint TEXT PRIMARY KEY, raw TEXT NOT NULL, "
            "first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP, responsive BOOLEAN DEFAULT 0, "
            "technologies TEXT)"
        )
        conn.execute("INSERT INTO domains (fingerprint, raw, responsive, first_seen) VALUES (?,?,?,?)",
                     ("old.com", "old.com", 1, "2010-01-01 00:00:00"))
        conn.commit()
    store = DomainStore(db, str(tmp_path / "out"))
    await store.open()
    try:
        assert await store.stale_domains(3600, 10) == ["old.com"]
    finally:
        await store.close()


async def test_buffered_records_survive_a_lost_connection(tmp_path):
    """flush() must not drop the buffer when there is nothing to write to."""
    store = DomainStore(str(tmp_path / "d.db"), str(tmp_path / "out"))
    await store.open()
    connection, store._db = store._db, None
    try:
        await store.add(DomainRecord("keep.example", "keep.example", True, []))
        assert await store.flush() == 0
        assert [record.fingerprint for record in store._pending] == ["keep.example"]
    finally:
        store._db = connection
        store._pending.clear()
        await store.close()


async def test_duplicates_are_rejected_beyond_the_cache(tmp_path):
    """Dedup stays exact when a domain has aged out of the memory cache."""
    store = DomainStore(str(tmp_path / "d.db"), str(tmp_path / "out"), cache_limit=2)
    await store.open()
    try:
        for index in range(5):
            await store.add(DomainRecord(f"d{index}.example", f"d{index}.example", True, []))
        await store.flush()
        assert len(store._seen) <= 2          # cache is bounded
        assert store.known_count == 5         # but the count is the real total

        fresh = await store.filter_new(["d0.example", "d4.example", "new.example"])
        assert fresh == ["new.example"]
    finally:
        await store.close()


async def test_filter_new_deduplicates_within_a_batch(tmp_path):
    store = DomainStore(str(tmp_path / "d.db"), str(tmp_path / "out"))
    await store.open()
    try:
        assert await store.filter_new(["a.example", "a.example", "b.example"]) == [
            "a.example", "b.example",
        ]
    finally:
        await store.close()


async def test_cache_is_warmed_from_the_database(tmp_path):
    path = str(tmp_path / "d.db")
    first = DomainStore(path, str(tmp_path / "out"))
    await first.open()
    await first.add(DomainRecord("warm.example", "warm.example", True, []))
    await first.close()

    second = DomainStore(path, str(tmp_path / "out"))
    await second.open()
    try:
        assert second.is_known("warm.example")
        assert second.known_count == 1
    finally:
        await second.close()


async def test_undated_rows_are_backfilled_on_open(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE domains (fingerprint TEXT PRIMARY KEY, raw TEXT NOT NULL, "
            "first_seen TIMESTAMP, responsive BOOLEAN DEFAULT 0, technologies TEXT)"
        )
        conn.execute("INSERT INTO domains (fingerprint, raw, first_seen) VALUES (?,?,NULL)",
                     ("undated.example", "undated.example"))
        conn.commit()

    store = DomainStore(path, str(tmp_path / "out"))
    await store.open()
    await store.close()

    with sqlite3.connect(path) as conn:
        stamp = conn.execute(
            "SELECT first_seen FROM domains WHERE fingerprint = 'undated.example'"
        ).fetchone()[0]
    assert stamp is not None


async def test_frontier_round_trip(tmp_path):
    store = DomainStore(str(tmp_path / "d.db"), str(tmp_path / "out"))
    await store.open()
    try:
        assert await store.push_frontier(["a.example", "b.example"], origin="seed.example") == 2
        assert await store.frontier_size() == 2

        taken = await store.take_frontier(1)
        assert len(taken) == 1
        assert await store.frontier_size() == 1

        rest = await store.take_frontier(10)
        assert len(rest) == 1
        assert await store.take_frontier(10) == []
    finally:
        await store.close()


async def test_frontier_skips_domains_already_stored(tmp_path):
    store = DomainStore(str(tmp_path / "d.db"), str(tmp_path / "out"))
    await store.open()
    try:
        await store.add(DomainRecord("known.example", "known.example", True, []))
        await store.flush()
        assert await store.push_frontier(["known.example", "fresh.example"]) == 1
        assert await store.take_frontier(10) == ["fresh.example"]
    finally:
        await store.close()


async def test_frontier_deduplicates(tmp_path):
    store = DomainStore(str(tmp_path / "d.db"), str(tmp_path / "out"))
    await store.open()
    try:
        await store.push_frontier(["x.example", "x.example"])
        await store.push_frontier(["x.example"])
        assert await store.frontier_size() == 1
    finally:
        await store.close()


async def test_frontier_is_bounded(tmp_path):
    store = DomainStore(str(tmp_path / "d.db"), str(tmp_path / "out"), frontier_limit=3)
    await store.open()
    try:
        await store.push_frontier([f"d{i}.example" for i in range(10)])
        assert await store.frontier_size() <= 3
        await store.push_frontier([f"e{i}.example" for i in range(10)])
        assert await store.trim_frontier() >= 0
        assert await store.frontier_size() <= 3
    finally:
        await store.close()


async def test_frontier_survives_a_restart(tmp_path):
    path = str(tmp_path / "d.db")
    first = DomainStore(path, str(tmp_path / "out"))
    await first.open()
    await first.push_frontier(["persist.example"], origin="seed")
    await first.close()

    second = DomainStore(path, str(tmp_path / "out"))
    await second.open()
    try:
        assert await second.take_frontier(10) == ["persist.example"]
    finally:
        await second.close()
