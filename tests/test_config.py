import json

import pytest

from domaincollector.config import Config, ConfigError


def test_defaults_are_valid():
    config = Config().validate()
    assert config.concurrency > 0
    assert config.sources


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"concurrency": 0}, "concurrency"),
        ({"concurrency": "abc"}, "concurrency"),
        ({"http_timeout": 0}, "http_timeout"),
        ({"fetch_interval": 1}, "fetch_interval"),
        ({"log_level": "LOUD"}, "log_level"),
        ({"db_path": "  "}, "db_path"),
        ({"nonsense": 1}, "unknown configuration key"),
    ],
)
def test_invalid_values_are_rejected(payload, message):
    with pytest.raises(ConfigError) as excinfo:
        Config.from_dict(payload)
    assert message in str(excinfo.value)


def test_sources_accept_a_comma_string_and_deduplicate():
    config = Config.from_dict({"sources": "crtsh, tranco ,crtsh"})
    assert config.sources == ["crtsh", "tranco"]


def test_save_and_load_round_trip(workdir):
    path = workdir / "cfg.json"
    original = Config(concurrency=42, sources=["tranco"], log_level="DEBUG")
    original.save(str(path))
    loaded = Config.load(str(path))
    assert loaded.concurrency == 42
    assert loaded.sources == ["tranco"]
    assert loaded.log_level == "DEBUG"
    assert json.loads(path.read_text())["concurrency"] == 42
    assert not (workdir / "cfg.json.tmp").exists()


def test_load_missing_file_returns_defaults(workdir):
    assert Config.load(str(workdir / "absent.json")).concurrency == Config().concurrency


def test_load_broken_file_raises(workdir):
    path = workdir / "broken.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        Config.load(str(path))


def test_certstream_url_must_be_a_websocket():
    with pytest.raises(ConfigError, match="ws://"):
        Config.from_dict({"certstream_url": "https://example.com"})
    assert Config.from_dict({"certstream_url": "ws://host:1/x"}).certstream_url == "ws://host:1/x"


def test_recheck_only_needs_a_recheck_interval():
    with pytest.raises(ConfigError, match="recheck_after"):
        Config.from_dict({"recheck_only": True})
    assert Config.from_dict({"recheck_only": True, "recheck_after": 60}).recheck_only is True


def test_recheck_defaults_to_disabled():
    assert Config().validate().recheck_after == 0
