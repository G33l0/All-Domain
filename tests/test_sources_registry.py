import pytest

from domainatlas.config import Config
from domainatlas.sources import (
    OnionIndexSource,
    Source,
    available_sources,
    build_sources,
    register_source,
)


class DummySource(Source):
    name = "dummy-feed"

    async def fetch(self, session, limit):
        return []


def test_builtin_sources_are_listed():
    names = available_sources()
    for expected in ("crtsh", "certstream", "tranco", "umbrella", "majestic", "onion", "file"):
        assert expected in names


def test_registering_a_source_makes_it_usable():
    register_source(DummySource)
    assert "dummy-feed" in available_sources()
    config = Config(sources=["dummy-feed"]).validate()
    assert [source.name for source in build_sources(config)] == ["dummy-feed"]


def test_registry_rejects_non_sources():
    with pytest.raises(TypeError):
        register_source(object)

    class Unnamed(Source):
        name = ""

    with pytest.raises(ValueError):
        register_source(Unnamed)


def test_onion_source_is_configured_from_the_config():
    config = Config(sources=["onion"], onion_index_url="https://example.invalid/list").validate()
    source = build_sources(config)[0]
    assert isinstance(source, OnionIndexSource)
    assert source.url == "https://example.invalid/list"


def test_onion_addresses_are_extracted_from_html():
    source = OnionIndexSource(user_agent="test")
    html = (
        "<a href='http://" + "a" * 56 + ".onion'>x</a>"
        "<li>" + "b" * 16 + ".onion</li>"
        "<span>not-an-address.onion</span>"
    )
    found = sorted(match.group(0) for match in source.ONION_RE.finditer(html))
    assert found == ["a" * 56 + ".onion", "b" * 16 + ".onion"]
