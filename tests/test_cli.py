import json

import pytest

from domaincollector.cli import apply_overrides, build_parser, main
from domaincollector.config import Config


def test_defaults_parse():
    args = build_parser().parse_args([])
    config = apply_overrides(Config(), args)
    assert config.concurrency == Config().concurrency


def test_overrides_are_applied():
    args = build_parser().parse_args(
        ["--concurrency", "5", "--timeout", "3.5", "--interval", "60", "--limit", "10",
         "--sources", "tranco,umbrella", "--responsive-only", "--no-tech-files", "--verify-ssl"]
    )
    config = apply_overrides(Config(), args)
    assert config.concurrency == 5
    assert config.http_timeout == 3.5
    assert config.fetch_interval == 60
    assert config.max_domains_per_cycle == 10
    assert config.sources == ["tranco", "umbrella"]
    assert config.store_unresponsive is False
    assert config.write_tech_files is False
    assert config.verify_ssl is True


def test_invalid_override_is_a_clean_error(capsys):
    with pytest.raises(SystemExit):
        main(["--concurrency", "0"])
    assert "concurrency" in capsys.readouterr().err


def test_save_config_writes_the_file(workdir, capsys):
    assert main(["--save-config", "--concurrency", "7", "-c", "my.json"]) == 0
    assert json.loads((workdir / "my.json").read_text())["concurrency"] == 7
    assert "my.json" in capsys.readouterr().out


def test_stats_without_a_database(workdir, capsys):
    assert main(["--stats", "--db", "missing.db"]) == 1
    assert "No database" in capsys.readouterr().out


def test_export_without_a_database(workdir, capsys):
    assert main(["--export", "Nginx", "--db", "missing.db"]) == 1
    assert "No database" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "domain-collector" in capsys.readouterr().out
