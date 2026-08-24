"""CertStreamSource, verified against a local certstream-shaped server."""

import asyncio
import json

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from domainatlas.config import Config
from domainatlas.sources import CertStreamSource, SourceError, _parse_certstream, build_sources

FULL_MESSAGE = json.dumps(
    {
        "message_type": "certificate_update",
        "data": {
            "leaf_cert": {
                "all_domains": ["a.example", "*.b.example"],
                "subject": {"CN": "c.example"},
            }
        },
    }
)


def test_parses_full_certstream_message():
    assert _parse_certstream(FULL_MESSAGE) == ["a.example", "*.b.example", "c.example"]


def test_parses_domains_only_shapes():
    assert _parse_certstream(json.dumps({"data": ["x.com", "y.com"]})) == ["x.com", "y.com"]
    assert _parse_certstream(json.dumps(["p.com"])) == ["p.com"]
    assert _parse_certstream("q.com\nr.com\n") == ["q.com", "r.com"]


def test_ignores_heartbeats_and_garbage():
    assert _parse_certstream(json.dumps({"message_type": "heartbeat", "timestamp": 1})) == []
    assert _parse_certstream("{not json") == []
    assert _parse_certstream("") == []
    assert _parse_certstream(json.dumps({"data": {"leaf_cert": {}}})) == []


@pytest_asyncio.fixture
async def stream_server():
    """A websocket server that replays certstream messages."""
    state = {"messages": [FULL_MESSAGE], "connections": 0, "close_after": None, "silent": False}

    async def handler(request):
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        state["connections"] += 1
        if not state["silent"]:
            for message in state["messages"]:
                await websocket.send_str(message)
        if state["close_after"] is not None:
            await asyncio.sleep(state["close_after"])
            await websocket.close()
            return websocket
        try:
            async for _ in websocket:
                pass
        except asyncio.CancelledError:  # pragma: no cover - shutdown race
            pass
        return websocket

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    state["url"] = f"ws://127.0.0.1:{port}/"
    try:
        yield state
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


async def test_streams_and_normalises_domains(stream_server, session):
    source = CertStreamSource(url=stream_server["url"], wait=10, user_agent="test")
    try:
        domains = await source.fetch(session, limit=10)
    finally:
        await source.close()
    # wildcards stripped, everything lower-cased and validated
    assert domains == ["a.example", "b.example", "c.example"]
    assert source.received == 3


async def test_buffer_is_drained_across_fetches(stream_server, session):
    source = CertStreamSource(url=stream_server["url"], wait=10, user_agent="test")
    try:
        first = await source.fetch(session, limit=2)
        second = await source.fetch(session, limit=2)
    finally:
        await source.close()
    assert len(first) == 2
    assert second == ["c.example"]
    assert not set(first) & set(second)


async def test_buffer_is_bounded(stream_server, session):
    stream_server["messages"] = [json.dumps({"data": [f"d{i}.example" for i in range(50)]})]
    source = CertStreamSource(url=stream_server["url"], wait=10, buffer_size=10, user_agent="test")
    try:
        domains = await source.fetch(session, limit=100)
    finally:
        await source.close()
    assert len(domains) == 10
    assert domains[-1] == "d49.example"  # the newest names win


async def test_silent_stream_raises_source_error(stream_server, session):
    stream_server["silent"] = True
    source = CertStreamSource(url=stream_server["url"], wait=1.5, user_agent="test")
    try:
        with pytest.raises(SourceError, match="no certificates received"):
            await source.fetch(session, limit=5)
    finally:
        await source.close()


async def test_unreachable_server_raises_source_error(session):
    source = CertStreamSource(url="ws://127.0.0.1:1/", wait=2, user_agent="test")
    try:
        with pytest.raises(SourceError, match="certstream"):
            await source.fetch(session, limit=5)
    finally:
        await source.close()


async def test_reconnects_after_the_server_hangs_up(stream_server, session):
    stream_server["close_after"] = 0.1
    source = CertStreamSource(url=stream_server["url"], wait=5, user_agent="test")
    try:
        assert await source.fetch(session, limit=10)
        await asyncio.sleep(2.5)  # backoff is 2s after the first drop
        assert stream_server["connections"] >= 2
    finally:
        await source.close()


async def test_close_stops_the_reader_task(stream_server, session):
    source = CertStreamSource(url=stream_server["url"], wait=5, user_agent="test")
    await source.fetch(session, limit=1)
    reader = source._reader
    await source.close()
    assert reader.done()
    await source.close()  # idempotent


def test_build_sources_passes_the_configured_url():
    config = Config(sources=["certstream"], certstream_url="ws://localhost:9999/x",
                    certstream_buffer=123, certstream_wait=7).validate()
    source = build_sources(config)[0]
    assert isinstance(source, CertStreamSource)
    assert source.url == "ws://localhost:9999/x"
    assert source.buffer_size == 123
    assert source.wait == 7
