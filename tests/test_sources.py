import io
import json
import zipfile

import pytest

from domaincollector.config import Config
from domaincollector.sources import (
    CrtShSource,
    SeedFileSource,
    SourceError,
    TrancoSource,
    _parse_crtsh,
    build_sources,
)


def test_parse_crtsh_json():
    payload = json.dumps(
        [{"name_value": "a.com\n*.b.a.com"}, {"common_name": "c.org"}]
    ).encode()
    assert _parse_crtsh(payload) == ["a.com", "*.b.a.com", "c.org"]


def test_parse_crtsh_salvages_truncated_json():
    payload = json.dumps([{"name_value": "a.com"}, {"name_value": "b.com"}, {"name_value": "c"}]).encode()
    salvaged = _parse_crtsh(payload[:60])
    assert "a.com" in salvaged and "b.com" in salvaged


def test_parse_crtsh_handles_garbage():
    assert _parse_crtsh(b"<html>502 Bad Gateway</html>") == []


def _zip_payload(rows):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("top-1m.csv", "\n".join(f"{i + 1},{row}" for i, row in enumerate(rows)))
    return buffer.getvalue()


def test_ranking_list_extract(tmp_path):
    source = TrancoSource(user_agent="test", cache_dir=str(tmp_path))
    rows = source._extract(_zip_payload(["a.com", "b.com", "c.com"]))
    assert rows == ["a.com", "b.com", "c.com"]


def test_ranking_list_rejects_corrupt_archive(tmp_path):
    source = TrancoSource(user_agent="test", cache_dir=str(tmp_path))
    with pytest.raises(SourceError):
        source._extract(b"definitely not a zip")


async def test_ranking_list_walks_the_list_between_cycles(tmp_path):
    source = TrancoSource(user_agent="test", cache_dir=str(tmp_path))
    source._write_cache(source.cache_name, _zip_payload([f"d{i}.com" for i in range(10)]))
    first = await source.fetch(session=None, limit=4)
    second = await source.fetch(session=None, limit=4)
    assert first == ["d0.com", "d1.com", "d2.com", "d3.com"]
    assert second == ["d4.com", "d5.com", "d6.com", "d7.com"]
    assert not set(first) & set(second)


async def test_ranking_list_replaces_a_corrupt_cache(tmp_path):
    source = TrancoSource(user_agent="test", cache_dir=str(tmp_path))
    source._write_cache(source.cache_name, b"corrupted")
    good = _zip_payload(["fresh.com"])

    async def fake_download(session):
        return good

    source._download = fake_download
    assert await source.fetch(session=None, limit=1) == ["fresh.com"]
    # the repaired payload is cached for the next cycle
    assert source._read_cache(source.cache_name, 3600) == good


async def test_seed_file_source(tmp_path):
    path = tmp_path / "seeds.txt"
    path.write_text("# comment\nexample.com\n\nhttps://Other.ORG/x\nnot a domain\n")
    source = SeedFileSource(str(path), user_agent="test", cache_dir=str(tmp_path))
    assert await source.fetch(session=None, limit=10) == ["example.com", "other.org"]


async def test_seed_file_missing_raises(tmp_path):
    source = SeedFileSource(str(tmp_path / "nope.txt"), user_agent="test", cache_dir=str(tmp_path))
    with pytest.raises(SourceError):
        await source.fetch(session=None, limit=5)


def test_build_sources_defaults():
    assert [source.name for source in build_sources(Config())] == ["crtsh", "tranco", "umbrella"]


def test_build_sources_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown source"):
        build_sources(Config(sources=["does-not-exist"]))


def test_build_sources_requires_seed_file_for_file_source():
    with pytest.raises(ValueError, match="seed_file"):
        build_sources(Config(sources=["file"]))


def test_seed_file_is_added_automatically():
    config = Config(sources=["crtsh"], seed_file="seeds.txt")
    assert [source.name for source in build_sources(config)] == ["file", "crtsh"]


def test_crtsh_queries_are_url_encoded():
    assert all(query.startswith("%25.") for query in CrtShSource.QUERIES)
