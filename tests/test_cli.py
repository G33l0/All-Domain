import json

import pytest

from domainatlas.cli import apply_overrides, build_parser, main
from domainatlas.config import Config


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
    assert "domain-atlas" in capsys.readouterr().out


def test_recheck_and_certstream_overrides():
    args = build_parser().parse_args(
        ["--recheck-after", "86400", "--recheck-batch", "50",
         "--certstream-url", "ws://localhost:8080/domains-only", "--sources", "certstream"]
    )
    config = apply_overrides(Config(), args)
    assert config.recheck_after == 86400
    assert config.recheck_batch == 50
    assert config.certstream_url == "ws://localhost:8080/domains-only"
    assert config.sources == ["certstream"]


def test_bad_certstream_url_is_rejected(capsys):
    with pytest.raises(SystemExit):
        main(["--certstream-url", "https://not-a-websocket"])
    assert "certstream_url" in capsys.readouterr().err


def test_recheck_only_requires_a_recheck_interval(capsys):
    with pytest.raises(SystemExit):
        main(["--recheck-only"])
    assert "recheck_after" in capsys.readouterr().err


def test_recheck_only_is_accepted_with_an_interval():
    args = build_parser().parse_args(["--recheck-only", "--recheck-after", "3600"])
    config = apply_overrides(Config(), args)
    assert config.recheck_only is True
    assert config.recheck_after == 3600


def test_tor_and_onion_overrides():
    args = build_parser().parse_args(
        ["--tor-proxy", "socks5://127.0.0.1:9050",
         "--onion-index-url", "https://example.org/onions"]
    )
    config = apply_overrides(Config(), args)
    assert config.tor_proxy == "socks5://127.0.0.1:9050"
    assert config.onion_index_url == "https://example.org/onions"


def test_tor_proxy_must_be_socks(capsys):
    with pytest.raises(SystemExit):
        main(["--tor-proxy", "http://proxy.example"])
    assert "socks5" in capsys.readouterr().err


def test_export_filters_build_a_query_filter():
    from domainatlas.cli import filter_from_args

    args = build_parser().parse_args(
        ["--export", "-", "--filter-technology", "Nginx", "--filter-source", "crtsh",
         "--filter-contains", "shop", "--filter-since", "2026-01-01", "--filter-live",
         "--filter-onion"]
    )
    criteria = filter_from_args(args)
    assert criteria.technology == "Nginx"
    assert criteria.source == "crtsh"
    assert criteria.text == "shop"
    assert criteria.since == "2026-01-01"
    assert criteria.responsive is True
    assert criteria.onion is True


def test_conflicting_export_filters_are_rejected():
    from domainatlas.cli import filter_from_args

    args = build_parser().parse_args(["--filter-live", "--filter-down"])
    with pytest.raises(ValueError, match="mutually exclusive"):
        filter_from_args(args)

    args = build_parser().parse_args(["--filter-onion", "--filter-clearnet"])
    with pytest.raises(ValueError, match="mutually exclusive"):
        filter_from_args(args)


def test_export_without_a_database_reports_cleanly(workdir, capsys):
    assert main(["--export", "out.csv", "--db", "missing.db"]) == 1
    assert "No database" in capsys.readouterr().err


def test_theme_names_are_validated(capsys):
    with pytest.raises(SystemExit):
        main(["--theme", "chartreuse"])
    assert "--theme must be one of" in capsys.readouterr().err
