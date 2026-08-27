import sqlite3

import pytest

from domainatlas.query import ORDER_NAME, DomainFilter, DomainQuery, QueryError


@pytest.fixture
def database(tmp_path):
    path = str(tmp_path / "atlas.db")
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE domains (
            fingerprint TEXT PRIMARY KEY, raw TEXT NOT NULL,
            first_seen TIMESTAMP, responsive INTEGER NOT NULL DEFAULT 0,
            technologies TEXT, status_code INTEGER, scheme TEXT, error TEXT,
            elapsed_ms INTEGER, source TEXT, checked_at TIMESTAMP);
        CREATE TABLE domain_tech (
            fingerprint TEXT NOT NULL, technology TEXT NOT NULL, version TEXT,
            PRIMARY KEY (fingerprint, technology));
        """
    )
    rows = [
        ("a.example", 1, 200, "tranco", '["Nginx", "React"]', "2026-01-01 00:00:00"),
        ("b.example", 1, 200, "crtsh", '["Nginx"]', "2026-01-02 00:00:00"),
        ("c.example", 0, None, "crtsh", None, "2026-01-03 00:00:00"),
        ("d" * 56 + ".onion", 0, None, "onion", None, "2026-01-04 00:00:00"),
    ]
    for fingerprint, responsive, status, source, technologies, seen in rows:
        connection.execute(
            "INSERT INTO domains (fingerprint, raw, responsive, status_code, source, "
            "technologies, first_seen, checked_at) VALUES (?,?,?,?,?,?,?,?)",
            (fingerprint, fingerprint, responsive, status, source, technologies, seen, seen),
        )
    connection.executemany(
        "INSERT INTO domain_tech VALUES (?,?,?)",
        [("a.example", "Nginx", "1.24"), ("a.example", "React", None), ("b.example", "Nginx", None)],
    )
    connection.commit()
    connection.close()
    return path


def test_missing_database_raises():
    with pytest.raises(QueryError):
        DomainQuery("does-not-exist.db").open()


def test_summary(database):
    with DomainQuery(database) as query:
        assert query.summary() == {
            "total": 4, "responsive": 2, "technologies": 2, "onion": 1,
        }


def test_page_is_newest_first(database):
    with DomainQuery(database) as query:
        rows = query.page(limit=10)
    assert [row.fingerprint for row in rows][0].endswith(".onion")
    assert [row.fingerprint for row in rows][-1] == "a.example"


def test_page_by_name(database):
    with DomainQuery(database) as query:
        rows = query.page(limit=10, order=ORDER_NAME)
    assert [row.fingerprint for row in rows][:2] == ["a.example", "b.example"]


def test_keyset_pagination_walks_every_row_once(database):
    seen = []
    with DomainQuery(database) as query:
        cursor = None
        while True:
            rows = query.page(limit=2, after=cursor)
            if not rows:
                break
            seen.extend(row.fingerprint for row in rows)
            cursor = query.cursor_for(rows[-1])
    assert len(seen) == len(set(seen)) == 4


def test_technologies_are_parsed(database):
    with DomainQuery(database) as query:
        row = next(r for r in query.page(limit=10) if r.fingerprint == "a.example")
    assert row.technologies == ["Nginx", "React"]


def test_filter_by_technology(database):
    with DomainQuery(database) as query:
        rows = query.page(DomainFilter(technology="React"), limit=10)
        assert [row.fingerprint for row in rows] == ["a.example"]
        assert query.count(DomainFilter(technology="Nginx")) == (2, False)


def test_filter_by_source_status_and_text(database):
    with DomainQuery(database) as query:
        assert query.count(DomainFilter(source="crtsh"))[0] == 2
        assert query.count(DomainFilter(responsive=True))[0] == 2
        assert query.count(DomainFilter(responsive=False))[0] == 2
        assert query.count(DomainFilter(text="a.exa"))[0] == 1


def test_filter_by_network(database):
    with DomainQuery(database) as query:
        assert query.count(DomainFilter(onion=True))[0] == 1
        assert query.count(DomainFilter(onion=False))[0] == 3


def test_filter_since(database):
    with DomainQuery(database) as query:
        assert query.count(DomainFilter(since="2026-01-03"))[0] == 2


def test_count_is_capped(database):
    with DomainQuery(database) as query:
        assert query.count(cap=2) == (2, True)
        assert query.count(cap=100) == (4, False)


def test_facets(database):
    with DomainQuery(database) as query:
        assert query.technologies() == [("Nginx", 2), ("React", 1)]
        assert query.sources() == ["crtsh", "onion", "tranco"]


def test_iter_all_streams_every_match(database):
    with DomainQuery(database) as query:
        assert len(list(query.iter_all(batch_size=2))) == 4
        assert [row.fingerprint for row in query.iter_all(DomainFilter(source="onion"))][0].endswith(".onion")


def test_empty_filter_detection():
    assert DomainFilter().is_empty()
    assert not DomainFilter(text="x").is_empty()
    assert not DomainFilter(onion=False).is_empty()


def test_database_is_opened_read_only(database):
    with DomainQuery(database) as query:
        with pytest.raises(sqlite3.OperationalError):
            query.connection.execute("DELETE FROM domains")


def _undated_database(tmp_path):
    path = str(tmp_path / "undated.db")
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE domains (
            fingerprint TEXT PRIMARY KEY, raw TEXT NOT NULL, first_seen TIMESTAMP,
            responsive INTEGER NOT NULL DEFAULT 0, technologies TEXT, status_code INTEGER,
            scheme TEXT, error TEXT, elapsed_ms INTEGER, source TEXT, checked_at TIMESTAMP);
        CREATE TABLE domain_tech (fingerprint TEXT NOT NULL, technology TEXT NOT NULL,
            version TEXT, PRIMARY KEY (fingerprint, technology));
        """
    )
    for index in range(6):
        stamp = None if index % 2 else f"2026-01-0{index + 1} 00:00:00"
        connection.execute(
            "INSERT INTO domains (fingerprint, raw, first_seen) VALUES (?,?,?)",
            (f"d{index}.example", f"d{index}.example", stamp),
        )
    connection.commit()
    connection.close()
    return path


def test_rows_without_a_timestamp_are_still_reachable(tmp_path):
    """Legacy rows with no first_seen must not vanish from the paged view."""
    path = _undated_database(tmp_path)
    with DomainQuery(path) as query:
        assert query._has_undated is True
        seen = []
        cursor = None
        for _ in range(20):
            rows = query.page(limit=2, after=cursor)
            if not rows:
                break
            seen.extend(row.fingerprint for row in rows)
            cursor = query.cursor_for(rows[-1])
    assert len(seen) == len(set(seen)) == 6


def test_undated_rows_sort_last(tmp_path):
    path = _undated_database(tmp_path)
    with DomainQuery(path) as query:
        rows = query.page(limit=10)
    stamps = [row.first_seen for row in rows]
    assert stamps[:3] == ["2026-01-05 00:00:00", "2026-01-03 00:00:00", "2026-01-01 00:00:00"]
    assert stamps[3:] == [None, None, None]


@pytest.mark.parametrize(
    "needle,expected",
    [("a_b", ["a_b.example"]), ("%", []), ("a%b", []), ("axb", ["axb.example"])],
)
def test_search_treats_wildcards_literally(tmp_path, needle, expected):
    path = str(tmp_path / "wild.db")
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE domains (
            fingerprint TEXT PRIMARY KEY, raw TEXT NOT NULL, first_seen TIMESTAMP,
            responsive INTEGER NOT NULL DEFAULT 0, technologies TEXT, status_code INTEGER,
            scheme TEXT, error TEXT, elapsed_ms INTEGER, source TEXT, checked_at TIMESTAMP);
        CREATE TABLE domain_tech (fingerprint TEXT NOT NULL, technology TEXT NOT NULL,
            version TEXT, PRIMARY KEY (fingerprint, technology));
        """
    )
    for name in ("axb.example", "a_b.example", "plain.example"):
        connection.execute(
            "INSERT INTO domains (fingerprint, raw, first_seen) VALUES (?,?,'2026-01-01')",
            (name, name),
        )
    connection.commit()
    connection.close()

    with DomainQuery(path) as query:
        found = [row.fingerprint for row in query.page(DomainFilter(text=needle), limit=10)]
        assert found == expected
        assert query.count(DomainFilter(text=needle))[0] == len(expected)
