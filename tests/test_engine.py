"""Engine tests against a real local HTTP server."""

import asyncio

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from domainatlas.config import Config
from domainatlas.engine import Collector, probe_domain
from domainatlas.sources import Source
from domainatlas.store import DomainStore

HTML = (
    '<html><head><meta name="generator" content="WordPress 6.4.2">'
    '<script src="/wp-content/themes/x/jquery-3.6.0.min.js"></script></head><body>hi</body></html>'
)


async def _handler(request):
    if request.path == "/missing":
        return web.Response(status=404, text="nope", headers={"Server": "nginx/1.24.0"})
    if request.path == "/big":
        return web.Response(text="x" * (2 * 1024 * 1024), headers={"Server": "nginx/1.24.0"})
    if request.path == "/slow":
        await asyncio.sleep(5)
        return web.Response(text="late")
    return web.Response(
        text=HTML,
        headers={"Server": "nginx/1.24.0", "X-Powered-By": "PHP/8.2.1",
                 "Set-Cookie": "PHPSESSID=abc; Path=/"},
    )


@pytest_asyncio.fixture
async def server():
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", _handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


async def test_probe_detects_technologies_over_http(server, session):
    result = await probe_domain(session, server, Config(http_timeout=5))
    assert result.responsive and result.reached
    assert result.status_code == 200
    assert result.scheme == "http"  # https is tried first and fails
    assert {"Nginx", "PHP", "WordPress", "jQuery"} <= set(result.technologies)
    assert result.versions["WordPress"] == "6.4.2"
    assert result.elapsed_ms >= 0


async def test_probe_keeps_fingerprints_from_error_responses(server, session):
    result = await probe_domain(session, f"{server}/missing", Config(http_timeout=5))
    assert result.reached is True
    assert result.responsive is False
    assert result.status_code == 404
    assert "Nginx" in result.technologies


async def test_probe_caps_the_body_size(server, session):
    config = Config(http_timeout=10, max_body_bytes=2048)
    result = await probe_domain(session, f"{server}/big", config)
    assert result.responsive is True  # no TypeError, no runaway download


async def test_probe_reports_unreachable_hosts(session):
    result = await probe_domain(session, "127.0.0.1:1", Config(http_timeout=3))
    assert result.responsive is False
    assert result.reached is False
    assert result.error
    assert "ssl:" not in result.error  # error text stays readable


async def test_probe_reports_timeouts(server, session):
    result = await probe_domain(session, f"{server}/slow", Config(http_timeout=1))
    assert result.responsive is False
    assert result.error == "timeout"


class StubSource(Source):
    name = "stub"

    def __init__(self, domains, **kwargs):
        kwargs.setdefault("user_agent", "test")
        super().__init__(**kwargs)
        self.domains = list(domains)
        self.calls = 0

    async def fetch(self, session, limit):
        self.calls += 1
        return self.domains[:limit]


class FailingSource(Source):
    name = "broken"

    def __init__(self, **kwargs):
        kwargs.setdefault("user_agent", "test")
        super().__init__(**kwargs)
        self.calls = 0

    async def fetch(self, session, limit):
        from domainatlas.sources import SourceError

        self.calls += 1
        raise SourceError("feed is down")


async def test_full_cycle_persists_results(tmp_path, server):
    config = Config(
        concurrency=4, http_timeout=5, fetch_interval=10, max_domains_per_cycle=3,
        db_path=str(tmp_path / "d.db"), output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
    ).validate()
    events = []
    source = StubSource(["example.com", "example.org", "example.net"], cache_dir=str(tmp_path))
    # point every candidate at the local server
    collector = Collector(config, on_event=events.append, sources=[source])

    original_probe = probe_domain

    async def patched(session, domain, cfg, ssl_arg=None):
        return await original_probe(session, server, cfg, ssl_arg)

    import domainatlas.engine as engine_module

    engine_module.probe_domain = patched
    try:
        stats = await collector.run(cycles=1)
    finally:
        engine_module.probe_domain = original_probe

    assert stats.processed == 3
    assert stats.responsive == 3
    assert stats.new == 3
    assert stats.errors == 0
    assert stats.tech_counts["WordPress"] == 3

    store = DomainStore(config.db_path, config.output_dir)
    await store.open()
    try:
        assert (await store.summary())["total"] == 3
        assert set(await store.domains_for_technology("WordPress")) == {
            "example.com", "example.org", "example.net"}
    finally:
        await store.close()
    assert (tmp_path / "out" / "WordPress.txt").exists()
    assert any(event.kind == "cycle" for event in events)


async def test_failing_source_is_cooled_down_not_fatal(tmp_path):
    config = Config(
        concurrency=2, fetch_interval=10, db_path=str(tmp_path / "d.db"),
        output_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"),
    ).validate()
    events = []
    source = FailingSource(cache_dir=str(tmp_path))
    collector = Collector(config, on_event=events.append, sources=[source])
    stats = await collector.run(cycles=1)
    assert stats.processed == 0
    assert stats.source_fail["broken"] == 1
    assert any("unavailable" in event.message for event in events if event.kind == "log")
    assert collector.running is False


async def test_duplicates_are_never_queued_twice(tmp_path):
    config = Config(
        concurrency=2, fetch_interval=10, max_domains_per_cycle=10,
        db_path=str(tmp_path / "d.db"), output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
    ).validate()
    source = StubSource(["dup.com", "dup.com", "DUP.com", "https://dup.com/x"], cache_dir=str(tmp_path))
    collector = Collector(config, sources=[source])

    async def never_reachable(session, domain, cfg, ssl_arg=None):
        from domainatlas.engine import ProbeResult

        return ProbeResult(responsive=False, error="stubbed")

    import domainatlas.engine as engine_module

    original = engine_module.probe_domain
    engine_module.probe_domain = never_reachable
    try:
        stats = await collector.run(cycles=1)
    finally:
        engine_module.probe_domain = original

    assert stats.processed == 1
    assert stats.duplicates == 3


async def test_stop_shuts_everything_down(tmp_path):
    config = Config(
        concurrency=3, fetch_interval=3600, max_domains_per_cycle=1,
        db_path=str(tmp_path / "d.db"), output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
    ).validate()
    source = StubSource(["stop.example"], cache_dir=str(tmp_path))
    collector = Collector(config, sources=[source])

    async def stub(session, domain, cfg, ssl_arg=None):
        from domainatlas.engine import ProbeResult

        return ProbeResult(responsive=False, error="stubbed")

    import domainatlas.engine as engine_module

    original = engine_module.probe_domain
    engine_module.probe_domain = stub
    task = asyncio.create_task(collector.run())
    try:
        await asyncio.sleep(0.5)
        collector.stop()
        stats = await asyncio.wait_for(task, timeout=15)
    finally:
        engine_module.probe_domain = original

    assert collector.running is False
    assert stats.cycles >= 1
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not [t for t in leftover if "consumer" in (t.get_name() or "")]


async def test_pause_and_resume(tmp_path):
    config = Config(
        concurrency=1, fetch_interval=3600, db_path=str(tmp_path / "d.db"),
        output_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"),
    ).validate()
    collector = Collector(config, sources=[StubSource([], cache_dir=str(tmp_path))])
    collector.pause()
    assert collector.paused is True
    collector.resume()
    assert collector.paused is False


async def _run_one_cycle(collector, probe):
    import domainatlas.engine as engine_module

    original = engine_module.probe_domain
    engine_module.probe_domain = probe
    try:
        return await collector.run(cycles=1)
    finally:
        engine_module.probe_domain = original


def _recheck_config(tmp_path, **overrides):
    settings = dict(
        concurrency=2, http_timeout=5, fetch_interval=10, max_domains_per_cycle=5,
        db_path=str(tmp_path / "d.db"), output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
    )
    settings.update(overrides)
    return Config(**settings).validate()


async def test_stale_domains_are_requeued_and_updated(tmp_path):
    import sqlite3

    from domainatlas.engine import ProbeResult

    config = _recheck_config(tmp_path, recheck_after=3600, recheck_batch=10)
    source = StubSource(["site.example"], cache_dir=str(tmp_path))

    async def first_probe(session, domain, cfg, ssl_arg=None):
        return ProbeResult(responsive=True, reached=True, status_code=200,
                           scheme="https", technologies=["Nginx"], versions={"Nginx": "1.0"})

    stats = await _run_one_cycle(Collector(config, sources=[source]), first_probe)
    assert stats.new == 1 and stats.rechecked == 0

    # Age the row so it is due for a re-check.
    with sqlite3.connect(config.db_path) as conn:
        conn.execute("UPDATE domains SET checked_at = '2000-01-01 00:00:00'")
        conn.commit()

    async def second_probe(session, domain, cfg, ssl_arg=None):
        return ProbeResult(responsive=True, reached=True, status_code=200,
                           scheme="https", technologies=["Apache"], versions={"Apache": "2.4"})

    # The source offers nothing new, so everything probed is a re-check.
    empty_source = StubSource([], cache_dir=str(tmp_path))
    stats = await _run_one_cycle(Collector(config, sources=[empty_source]), second_probe)
    assert stats.rechecked == 1
    assert stats.new == 0
    assert stats.processed == 1

    store = DomainStore(config.db_path, config.output_dir)
    await store.open()
    try:
        assert (await store.summary())["total"] == 1  # updated, not duplicated
        assert await store.top_technologies() == [("Apache", 1)]
        assert await store.stale_domains(3600, 10) == []
    finally:
        await store.close()


async def test_rechecking_is_off_by_default(tmp_path):
    import sqlite3

    from domainatlas.engine import ProbeResult

    config = _recheck_config(tmp_path)
    assert config.recheck_after == 0

    async def probe(session, domain, cfg, ssl_arg=None):
        return ProbeResult(responsive=True, reached=True, status_code=200, technologies=[])

    await _run_one_cycle(Collector(config, sources=[StubSource(["a.example"], cache_dir=str(tmp_path))]), probe)
    with sqlite3.connect(config.db_path) as conn:
        conn.execute("UPDATE domains SET checked_at = '2000-01-01 00:00:00'")
        conn.commit()

    stats = await _run_one_cycle(Collector(config, sources=[StubSource([], cache_dir=str(tmp_path))]), probe)
    assert stats.processed == 0
    assert stats.rechecked == 0


async def test_recheck_batch_limits_the_queue(tmp_path):
    import sqlite3

    from domainatlas.engine import ProbeResult

    config = _recheck_config(tmp_path, recheck_after=3600, recheck_batch=2)

    async def probe(session, domain, cfg, ssl_arg=None):
        return ProbeResult(responsive=True, reached=True, status_code=200, technologies=[])

    seeds = [f"s{i}.example" for i in range(5)]
    await _run_one_cycle(Collector(config, sources=[StubSource(seeds, cache_dir=str(tmp_path))]), probe)
    with sqlite3.connect(config.db_path) as conn:
        conn.execute("UPDATE domains SET checked_at = '2000-01-01 00:00:00'")
        conn.commit()

    stats = await _run_one_cycle(Collector(config, sources=[StubSource([], cache_dir=str(tmp_path))]), probe)
    assert stats.rechecked == 2


async def test_sources_are_closed_on_shutdown(tmp_path):
    config = _recheck_config(tmp_path)

    class ClosableSource(StubSource):
        name = "closable"

        def __init__(self, **kwargs):
            super().__init__([], **kwargs)
            self.closed = False

        async def close(self):
            self.closed = True

    source = ClosableSource(cache_dir=str(tmp_path))
    await Collector(config, sources=[source]).run(cycles=1)
    assert source.closed is True


async def test_onion_domains_are_stored_when_tor_is_not_configured(tmp_path):
    """Hidden services must still be recorded, flagged as unprobed."""
    from domainatlas.store import DomainStore

    config = _recheck_config(tmp_path)
    assert config.tor_proxy == ""
    onion = "a" * 56 + ".onion"
    source = StubSource([onion, "clear.example"], cache_dir=str(tmp_path))
    collector = Collector(config, sources=[source])

    from domainatlas.engine import ProbeResult

    async def probe(session, domain, cfg, ssl_arg=None):
        return ProbeResult(responsive=True, reached=True, status_code=200, technologies=[])

    stats = await _run_one_cycle(collector, probe)
    assert stats.processed == 2

    store = DomainStore(config.db_path, config.output_dir)
    await store.open()
    try:
        from domainatlas.query import DomainFilter, DomainQuery

        await store.flush()
    finally:
        await store.close()

    with DomainQuery(config.db_path) as query:
        rows = query.page(DomainFilter(onion=True), limit=10)
        assert [row.fingerprint for row in rows] == [onion]
        assert rows[0].responsive is False
        assert "Tor proxy" in (rows[0].error or "")
        assert query.count(DomainFilter(onion=False))[0] == 1


async def test_tor_unavailable_is_reported_once(tmp_path):
    from domainatlas.engine import TorUnavailable

    config = _recheck_config(tmp_path)
    config.tor_proxy = "socks5://127.0.0.1:9"
    events = []
    collector = Collector(config, on_event=events.append,
                          sources=[StubSource([], cache_dir=str(tmp_path))])

    import domainatlas.engine as engine_module

    original = engine_module.tor_connector

    def unavailable(cfg):
        raise TorUnavailable("aiohttp-socks is not installed")

    engine_module.tor_connector = unavailable
    try:
        await collector.run(cycles=1)
    finally:
        engine_module.tor_connector = original

    messages = [event.message for event in events]
    assert any("Tor unavailable" in message for message in messages)


async def test_sources_share_the_cycle_budget(tmp_path):
    """A productive source must not starve the others."""
    from domainatlas.engine import ProbeResult

    config = _recheck_config(tmp_path, max_domains_per_cycle=9)
    first = StubSource([f"a{i}.example" for i in range(100)], cache_dir=str(tmp_path))
    second = StubSource([f"b{i}.example" for i in range(100)], cache_dir=str(tmp_path))
    third = StubSource([f"c{i}.example" for i in range(100)], cache_dir=str(tmp_path))
    first.name, second.name, third.name = "first", "second", "third"

    async def probe(session, domain, cfg, ssl_arg=None):
        return ProbeResult(responsive=True, reached=True, status_code=200, technologies=[])

    collector = Collector(config, sources=[first, second, third])
    stats = await _run_one_cycle(collector, probe)
    assert stats.processed == 9

    from domainatlas.query import DomainQuery

    with DomainQuery(config.db_path) as query:
        stored = {row.fingerprint[0] for row in query.page(limit=50)}
    assert stored == {"a", "b", "c"}


def test_hosts_are_harvested_from_page_markup():
    from domainatlas.engine import hosts_in_html

    html = (
        '<a href="https://partner.example/path">x</a>'
        '<script src="//cdn.jsdelivr.net/lib.js"></script>'
        '<img src="http://images.other.example/a.png">'
        '<link href="https://fonts.googleapis.com/css">'
        'plain text https://mentioned.example/page'
    )
    found = hosts_in_html(html)
    assert "partner.example" in found
    assert "images.other.example" in found
    assert "mentioned.example" in found
    # Ubiquitous asset hosts add nothing to an inventory.
    assert "cdn.jsdelivr.net" not in found
    assert "fonts.googleapis.com" not in found


def test_html_harvest_is_bounded_and_safe():
    from domainatlas.engine import hosts_in_html

    assert hosts_in_html("") == []
    assert hosts_in_html(None) == []
    crowded = " ".join(f'<a href="https://h{i}.example/">x</a>' for i in range(500))
    assert len(hosts_in_html(crowded, limit=25)) == 25


def test_certificate_harvest_tolerates_a_missing_connection():
    from domainatlas.engine import hosts_in_certificate

    assert hosts_in_certificate(None) == []

    class Useless:
        def getpeercert(self, binary_form=False):
            raise OSError("not connected")

    assert hosts_in_certificate(Useless()) == []

    class Empty:
        def getpeercert(self, binary_form=False):
            return b""

    assert hosts_in_certificate(Empty()) == []


async def test_discoveries_feed_the_frontier(tmp_path):
    """A probe's own findings become candidates for the next cycle."""
    from domainatlas.engine import ProbeResult
    from domainatlas.store import DomainStore

    config = _recheck_config(tmp_path)
    source = StubSource(["seed.example"], cache_dir=str(tmp_path))
    collector = Collector(config, sources=[source])

    async def probe(session, domain, cfg, ssl_arg=None):
        return ProbeResult(
            responsive=True, reached=True, status_code=200, technologies=[],
            discovered=["found-a.example", "found-b.example", "seed.example", "not a domain"],
        )

    stats = await _run_one_cycle(collector, probe)
    assert stats.discovered == 2          # the origin and the junk are dropped

    store = DomainStore(config.db_path, config.output_dir)
    await store.open()
    try:
        queued = await store.take_frontier(10)
        assert sorted(queued) == ["found-a.example", "found-b.example"]
    finally:
        await store.close()


async def test_self_source_runs_without_any_network(tmp_path):
    from domainatlas.sources import SelfExpansionSource, SourceError
    from domainatlas.store import DomainStore

    store = DomainStore(str(tmp_path / "d.db"), str(tmp_path / "out"))
    await store.open()
    try:
        source = SelfExpansionSource(store=store, user_agent="test", cache_dir=str(tmp_path))

        with pytest.raises(SourceError, match="nothing discovered yet"):
            await source.fetch(session=None, limit=5)

        await store.push_frontier(["a.example", "b.example"])
        assert sorted(await source.fetch(session=None, limit=5)) == ["a.example", "b.example"]
    finally:
        await store.close()


async def test_an_empty_source_is_not_put_on_cooldown(tmp_path):
    """A healthy source with nothing queued yet must not be penalised.

    The self-expansion frontier is empty on a cold start and fills within
    seconds; a failure cooldown would keep it idle for a minute.
    """
    from domainatlas.engine import ProbeResult
    from domainatlas.sources import SourceEmpty

    class EmptyThenReady(Source):
        name = "eventually"

        def __init__(self, **kwargs):
            kwargs.setdefault("user_agent", "test")
            super().__init__(**kwargs)
            self.calls = 0

        async def fetch(self, session, limit):
            self.calls += 1
            if self.calls == 1:
                raise SourceEmpty("nothing yet")
            return ["ready.example"]

    config = _recheck_config(tmp_path, fetch_interval=10)
    source = EmptyThenReady(cache_dir=str(tmp_path))
    events = []
    collector = Collector(config, on_event=events.append, sources=[source])

    async def probe(session, domain, cfg, ssl_arg=None):
        return ProbeResult(responsive=True, reached=True, status_code=200)

    await _run_one_cycle(collector, probe)
    assert source.calls == 1
    assert collector.stats.source_fail["eventually"] == 0
    assert "eventually" not in collector._cooldowns
    assert not any("pausing it" in event.message for event in events)

    stats = await _run_one_cycle(collector, probe)
    assert source.calls == 2
    assert stats.processed == 1
