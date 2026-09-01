import asyncio
import json
import sqlite3

import pytest

from domainatlas.export import ExportError, export_to_path, format_for_path, write_rows
from domainatlas.query import DomainFilter, DomainRow
from domainatlas.store import DomainRecord, DomainStore


@pytest.fixture
def database(tmp_path):
    """Built by the store, so the export always reads the shipping schema."""
    path = str(tmp_path / "atlas.db")

    async def build():
        async with DomainStore(path, write_tech_files=False) as store:
            await store.add(DomainRecord(
                "a.example", True, ["Nginx", "React"], versions={"Nginx": "1.24"},
                status_code=200, source="tranco",
            ))
            await store.add(DomainRecord("b.example", False, [], source="crtsh", error="timeout"))
            await store.flush()

    asyncio.run(build())
    connection = sqlite3.connect(path)
    connection.execute("UPDATE domains SET first_seen='2026-01-01' WHERE fingerprint='a.example'")
    connection.execute("UPDATE domains SET first_seen='2026-01-02' WHERE fingerprint='b.example'")
    connection.commit()
    connection.close()
    return path


def test_format_inference():
    assert format_for_path("out.csv") == "csv"
    assert format_for_path("out.jsonl") == "jsonl"
    assert format_for_path("out.txt") == "txt"
    assert format_for_path("out.unknown") == "csv"
    assert format_for_path("out.unknown", fallback="txt") == "txt"


def test_csv_export(tmp_path, database):
    destination = tmp_path / "out.csv"
    assert export_to_path(database, str(destination), export_format="csv") == 2
    lines = destination.read_text().splitlines()
    assert lines[0].startswith("domain,site,hosts,responsive,status_code")
    assert any(line.startswith("a.example,a.example,1,True,200") for line in lines)
    assert "Nginx React" in destination.read_text()


def test_json_export_is_valid(tmp_path, database):
    destination = tmp_path / "out.json"
    export_to_path(database, str(destination), export_format="json")
    payload = json.loads(destination.read_text())
    assert {row["domain"] for row in payload} == {"a.example", "b.example"}
    assert payload[0]["technologies"] == ["Nginx", "React"] or payload[1]["technologies"] == ["Nginx", "React"]


def test_jsonl_export(tmp_path, database):
    destination = tmp_path / "out.jsonl"
    export_to_path(database, str(destination), export_format="jsonl")
    rows = [json.loads(line) for line in destination.read_text().splitlines()]
    assert len(rows) == 2


def test_txt_export_is_domains_only(tmp_path, database):
    destination = tmp_path / "out.txt"
    export_to_path(database, str(destination), export_format="txt")
    assert destination.read_text().split() == ["a.example", "b.example"]


def test_export_applies_the_filter(tmp_path, database):
    destination = tmp_path / "filtered.txt"
    written = export_to_path(
        database, str(destination), DomainFilter(technology="Nginx"), "txt"
    )
    assert written == 1
    assert destination.read_text().strip() == "a.example"


def test_export_of_no_matches_writes_an_empty_file(tmp_path, database):
    destination = tmp_path / "none.txt"
    assert export_to_path(database, str(destination), DomainFilter(text="zzz"), "txt") == 0
    assert destination.read_text() == ""


def test_unknown_format_is_rejected(tmp_path, database):
    with pytest.raises(ExportError):
        export_to_path(database, str(tmp_path / "x.out"), export_format="xml")


def test_partial_file_is_cleaned_up(tmp_path, database):
    with pytest.raises(ExportError):
        export_to_path(database, str(tmp_path / "x.out"), export_format="xml")
    assert not list(tmp_path.glob("*.part"))


def test_write_rows_to_a_handle(tmp_path):
    rows = [
        DomainRow("x.example", True, 200, "https", "tranco", None, None, None, ["Nginx"]),
        DomainRow("y.example", False, None, None, "crtsh", "timeout", None, None, []),
    ]
    destination = tmp_path / "manual.csv"
    with open(destination, "w", encoding="utf-8", newline="") as handle:
        assert write_rows(rows, handle, "csv") == 2
    assert "x.example" in destination.read_text()


def test_progress_callback_is_called(tmp_path, database):
    seen = []
    export_to_path(database, str(tmp_path / "p.txt"), export_format="txt",
                   progress=seen.append)
    assert seen and seen[-1] == 2
