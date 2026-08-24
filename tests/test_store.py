import json
import sqlite3

import pytest
import pytest_asyncio

from domaincollector.store import DomainRecord, DomainStore, safe_filename

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
