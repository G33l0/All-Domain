"""Domain feeds.

Every source knows how to fetch a batch of candidate domains.  Sources fail
independently: a dead feed (crt.sh returns HTTP 502 for days at a time) raises
:class:`SourceError`, the engine records the failure, puts the source on a
cooldown and moves to the next one.

Large ranking lists (Tranco/Umbrella/Majestic) are cached on disk and consumed
in successive windows, so each cycle yields *new* domains instead of the same
first N entries over and over.
"""

from __future__ import annotations

import asyncio
import collections
import csv
import logging
import io
import json
import os
import random
import re
import time
import zipfile
from typing import Dict, List, Optional, Sequence

import aiohttp

from .domains import normalize_all, normalize_domain
from .httputil import read_capped

__all__ = [
    "Source",
    "SourceError",
    "SOURCE_CLASSES",
    "available_sources",
    "build_sources",
    "load_plugin_sources",
    "register_source",
]


class SourceError(RuntimeError):
    """A feed could not be read this cycle."""


async def _get(
    session: "aiohttp.ClientSession",
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    retries: int = 3,
    expect_json: bool = False,
) -> bytes:
    """GET *url* with retries, exponential backoff and a hard size cap."""
    last_error: Optional[str] = None
    for attempt in range(max(1, retries)):
        if attempt:
            await asyncio.sleep(min(2 ** attempt, 30))
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with session.get(url, timeout=client_timeout, allow_redirects=True) as response:
                if response.status != 200:
                    last_error = f"HTTP {response.status}"
                    if response.status in (400, 401, 403, 404, 410):
                        break  # not worth retrying
                    continue
                declared = response.content_length
                if declared is not None and declared > max_bytes:
                    raise SourceError(
                        f"{url.split('?')[0]} is {declared} bytes, over the {max_bytes} byte limit"
                    )
                payload = await read_capped(response, max_bytes)
                if not payload:
                    last_error = "empty response"
                    continue
                return payload
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    raise SourceError(f"{url.split('?')[0]} failed ({last_error})")


class Source:
    """Base class for a domain feed."""

    name = "source"
    #: Feeds that hand out huge static lists do not need to be polled often.
    slow = False

    def __init__(self, user_agent: str, cache_dir: str = ".cache", timeout: float = 30.0, retries: int = 3) -> None:
        self.user_agent = user_agent
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.retries = retries

    async def fetch(self, session: "aiohttp.ClientSession", limit: int) -> List[str]:
        raise NotImplementedError

    async def close(self) -> None:
        """Release anything long-lived (streams, tasks).  Safe to call twice."""

    # -------------------------------------------------------------- utilities
    def _cache_path(self, filename: str) -> str:
        os.makedirs(self.cache_dir, exist_ok=True)
        return os.path.join(self.cache_dir, filename)

    def _read_cache(self, filename: str, max_age: float) -> Optional[bytes]:
        path = self._cache_path(filename)
        try:
            if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < max_age:
                with open(path, "rb") as handle:
                    return handle.read()
        except OSError:
            return None
        return None

    def _drop_cache(self, filename: str) -> None:
        try:
            os.remove(self._cache_path(filename))
        except OSError:
            pass

    def _write_cache(self, filename: str, payload: bytes) -> None:
        path = self._cache_path(filename)
        try:
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as handle:
                handle.write(payload)
            os.replace(tmp, path)
        except OSError:
            pass

    def _offset_path(self) -> str:
        return self._cache_path(f"{self.name}.offset")

    def _next_offset(self, count: int, total_hint: int) -> int:
        """Walk through a big ranking list a window at a time, wrapping around."""
        offset = 0
        try:
            with open(self._offset_path(), "r", encoding="utf-8") as handle:
                offset = int(handle.read().strip() or 0)
        except (OSError, ValueError):
            offset = 0
        if total_hint and offset >= total_hint:
            offset = 0
        try:
            with open(self._offset_path(), "w", encoding="utf-8") as handle:
                handle.write(str(offset + count))
        except OSError:
            pass
        return offset


class CrtShSource(Source):
    """Certificate Transparency search at crt.sh.

    crt.sh is regularly overloaded; the original ``q=%.`` query was also
    invalid.  We rotate through per-TLD wildcard queries, tolerate a truncated
    body and fall back to a regex scan when the JSON is cut off by the size cap.
    """

    name = "crtsh"
    QUERIES = (
        "%25.com", "%25.net", "%25.org", "%25.io", "%25.dev", "%25.app",
        "%25.co", "%25.de", "%25.uk", "%25.fr", "%25.nl", "%25.se",
        "%25.cloud", "%25.ai", "%25.xyz", "%25.shop",
    )
    MAX_BYTES = 8 * 1024 * 1024

    async def fetch(self, session: "aiohttp.ClientSession", limit: int) -> List[str]:
        query = random.choice(self.QUERIES)
        url = f"https://crt.sh/?q={query}&output=json&exclude=expired"
        payload = await _get(
            session, url, timeout=max(self.timeout, 30.0), max_bytes=self.MAX_BYTES,
            retries=self.retries, expect_json=True,
        )
        names = _parse_crtsh(payload)
        if not names:
            raise SourceError("crt.sh returned no usable names")
        domains = normalize_all(names)
        random.shuffle(domains)
        return domains[:limit]


def _parse_crtsh(payload: bytes) -> List[str]:
    """Parse a crt.sh JSON body, salvaging names when the body is truncated."""
    text = payload.decode("utf-8", errors="ignore")
    names: List[str] = []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    for key in ("name_value", "common_name"):
                        value = entry.get(key)
                        if value:
                            names.extend(str(value).split("\n"))
            return names
    except json.JSONDecodeError:
        pass
    for match in re.finditer(r'"(?:name_value|common_name)"\s*:\s*"((?:[^"\\]|\\.)*)"', text):
        names.extend(match.group(1).replace("\\n", "\n").split("\n"))
    return names


class _RankingListSource(Source):
    """Shared logic for the big CSV/ZIP ranking lists."""

    url = ""
    cache_name = ""
    cache_ttl = 24 * 3600
    domain_column = 1
    has_header = False
    zipped = True
    MAX_BYTES = 64 * 1024 * 1024
    slow = True

    async def _download(self, session: "aiohttp.ClientSession") -> bytes:
        payload = await _get(
            session, self.url, timeout=max(self.timeout, 120.0),
            max_bytes=self.MAX_BYTES, retries=self.retries,
        )
        return payload

    async def fetch(self, session: "aiohttp.ClientSession", limit: int) -> List[str]:
        payload = self._read_cache(self.cache_name, self.cache_ttl)
        from_cache = payload is not None
        if payload is None:
            payload = await self._download(session)
        try:
            rows = await asyncio.to_thread(self._extract, payload)
        except SourceError:
            # A truncated or corrupted cache file must not poison every cycle.
            self._drop_cache(self.cache_name)
            if not from_cache:
                raise
            payload = await self._download(session)
            rows = await asyncio.to_thread(self._extract, payload)
        if not from_cache or not self._read_cache(self.cache_name, self.cache_ttl):
            self._write_cache(self.cache_name, payload)
        if not rows:
            raise SourceError(f"{self.name}: no rows in list")
        offset = self._next_offset(limit, len(rows))
        window = rows[offset:offset + limit]
        if len(window) < limit:
            window += rows[: limit - len(window)]
        return normalize_all(window)

    def _extract(self, payload: bytes) -> List[str]:
        try:
            if self.zipped:
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    inner = next((n for n in archive.namelist() if n.lower().endswith(".csv")), None)
                    if inner is None:
                        raise SourceError(f"{self.name}: archive has no CSV")
                    raw = archive.read(inner)
            else:
                raw = payload
        except (zipfile.BadZipFile, EOFError) as exc:
            raise SourceError(f"{self.name}: corrupt download ({exc})") from exc

        text = raw.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        rows: List[str] = []
        for index, fields in enumerate(reader):
            if not fields:
                continue
            if index == 0 and self.has_header:
                continue
            if len(fields) <= self.domain_column:
                continue
            rows.append(fields[self.domain_column].strip())
        return rows


class TrancoSource(_RankingListSource):
    name = "tranco"
    url = "https://tranco-list.eu/top-1m.csv.zip"
    cache_name = "tranco-top-1m.zip"
    domain_column = 1


class UmbrellaSource(_RankingListSource):
    name = "umbrella"
    url = "https://s3-us-west-1.amazonaws.com/umbrella-static/top-1m.csv.zip"
    cache_name = "umbrella-top-1m.zip"
    domain_column = 1


class MajesticSource(_RankingListSource):
    name = "majestic"
    url = "https://downloads.majestic.com/majestic_million.csv"
    cache_name = "majestic-million.csv"
    zipped = False
    has_header = True
    domain_column = 2


class CertStreamSource(Source):
    """Live Certificate Transparency feed over a certstream websocket.

    Unlike the polling sources this one holds a persistent connection: a
    background task appends every streamed name to a bounded buffer, and each
    fetch drains it.  That keeps the pull-based Source interface intact while
    the stream runs continuously between cycles.

    Both certstream message shapes are understood:

    * full     - ``{"message_type": "certificate_update",
                   "data": {"leaf_cert": {"all_domains": [...]}}}``
    * domains-only - ``{"data": ["a.com", "b.com"]}``, a bare JSON list, or
      plain newline-separated text.

    The public ``certstream.calidog.io`` server accepts connections but is
    frequently idle; point ``certstream_url`` at your own certstream-server-go
    instance for a dependable feed.
    """

    name = "certstream"

    def __init__(
        self,
        url: str = "wss://certstream.calidog.io/domains-only",
        buffer_size: int = 20000,
        wait: float = 15.0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.url = url
        self.buffer_size = max(1, int(buffer_size))
        self.wait = max(1.0, float(wait))
        self._buffer: "collections.deque[str]" = collections.deque(maxlen=self.buffer_size)
        self._reader: Optional[asyncio.Task] = None
        self._have_data = asyncio.Event()
        self._connected = False
        self._last_error: Optional[str] = None
        self._closing = False
        self.received = 0

    # ------------------------------------------------------------------ fetch
    async def fetch(self, session: "aiohttp.ClientSession", limit: int) -> List[str]:
        self._ensure_reader(session)
        if not self._buffer:
            # Give a freshly opened stream a chance to produce something.
            try:
                await asyncio.wait_for(self._have_data.wait(), timeout=self.wait)
            except asyncio.TimeoutError:
                pass
        if not self._buffer:
            if self._last_error:
                raise SourceError(f"certstream: {self._last_error}")
            raise SourceError(
                f"certstream: no certificates received from {self.url} within {self.wait:.0f}s"
            )
        drained: List[str] = []
        while self._buffer and len(drained) < limit:
            drained.append(self._buffer.popleft())
        if not self._buffer:
            self._have_data.clear()
        return drained

    def _ensure_reader(self, session: "aiohttp.ClientSession") -> None:
        if self._closing:
            return
        if self._reader is None or self._reader.done():
            self._reader = asyncio.create_task(self._stream(session), name="certstream-reader")

    async def close(self) -> None:
        self._closing = True
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass

    # ----------------------------------------------------------------- stream
    async def _stream(self, session: "aiohttp.ClientSession") -> None:
        """Stay connected, reconnecting with backoff until the source closes."""
        attempt = 0
        while not self._closing:
            try:
                async with session.ws_connect(
                    self.url, heartbeat=30, timeout=aiohttp.ClientWSTimeout(ws_close=30)
                ) as websocket:
                    self._connected = True
                    self._last_error = None
                    attempt = 0
                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            self._ingest(message.data)
                        elif message.type == aiohttp.WSMsgType.BINARY:
                            self._ingest(message.data.decode("utf-8", errors="ignore"))
                        elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        if self._closing:
                            break
                self._last_error = "stream closed by the server"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            self._connected = False
            if self._closing:
                break
            attempt += 1
            await asyncio.sleep(min(2 ** attempt, 60))

    def _ingest(self, payload: str) -> None:
        for candidate in _parse_certstream(payload):
            domain = normalize_domain(candidate)
            if domain:
                self._buffer.append(domain)
                self.received += 1
        if self._buffer:
            self._have_data.set()


def _parse_certstream(payload: str) -> List[str]:
    """Pull host names out of any certstream message shape."""
    payload = (payload or "").strip()
    if not payload:
        return []
    if payload[0] not in "{[":
        # domains-only servers may emit plain text
        return [line.strip() for line in payload.splitlines() if line.strip()]
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if isinstance(message, list):
        return [str(item) for item in message]
    if not isinstance(message, dict):
        return []
    if message.get("message_type") == "heartbeat":
        return []
    data = message.get("data")
    if isinstance(data, list):
        return [str(item) for item in data]
    names: List[str] = []
    if isinstance(data, dict):
        leaf = data.get("leaf_cert")
        if isinstance(leaf, dict):
            for key in ("all_domains", "domains"):
                values = leaf.get(key)
                if isinstance(values, list):
                    names.extend(str(item) for item in values)
            subject = leaf.get("subject")
            if isinstance(subject, dict) and subject.get("CN"):
                names.append(str(subject["CN"]))
        for key in ("all_domains", "domains"):
            values = data.get(key)
            if isinstance(values, list):
                names.extend(str(item) for item in values)
    return names


class OnionIndexSource(Source):
    """Tor hidden services listed by a public clearnet index.

    Addresses are collected over the clear web, so this source works without
    Tor. Probing the services themselves needs ``tor_proxy`` configured.
    """

    name = "onion"
    slow = True
    MAX_BYTES = 16 * 1024 * 1024
    ONION_RE = re.compile(r"\b([a-z2-7]{16}|[a-z2-7]{56})\.onion\b", re.IGNORECASE)

    def __init__(self, url: str = "https://ahmia.fi/onions/", **kwargs) -> None:
        super().__init__(**kwargs)
        self.url = url

    async def fetch(self, session: "aiohttp.ClientSession", limit: int) -> List[str]:
        payload = await _get(
            session, self.url, timeout=max(self.timeout, 60.0),
            max_bytes=self.MAX_BYTES, retries=self.retries,
        )
        text = payload.decode("utf-8", errors="ignore")
        addresses = [f"{match.group(0).lower()}" for match in self.ONION_RE.finditer(text)]
        if not addresses:
            raise SourceError(f"{self.url} listed no .onion addresses")
        unique = normalize_all(addresses)
        offset = self._next_offset(limit, len(unique))
        window = unique[offset:offset + limit]
        if len(window) < limit:
            window += unique[: limit - len(window)]
        return window


class SeedFileSource(Source):
    """Read candidate domains from a local file (one per line)."""

    name = "file"
    slow = True

    def __init__(self, path: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.path = path

    async def fetch(self, session: "aiohttp.ClientSession", limit: int) -> List[str]:
        if not self.path or not os.path.exists(self.path):
            raise SourceError(f"seed file not found: {self.path}")
        lines = await asyncio.to_thread(self._read_lines)
        if not lines:
            raise SourceError(f"seed file is empty: {self.path}")
        offset = self._next_offset(limit, len(lines))
        window = lines[offset:offset + limit]
        return normalize_all(window)

    def _read_lines(self) -> List[str]:
        with open(self.path, "r", encoding="utf-8", errors="ignore") as handle:
            return [line.strip() for line in handle if line.strip() and not line.startswith("#")]


SOURCE_CLASSES: Dict[str, type] = {
    CrtShSource.name: CrtShSource,
    CertStreamSource.name: CertStreamSource,
    TrancoSource.name: TrancoSource,
    UmbrellaSource.name: UmbrellaSource,
    MajesticSource.name: MajesticSource,
    OnionIndexSource.name: OnionIndexSource,
}

#: The seed-file source is built from a path rather than a bare name.
BUILTIN_EXTRA = ("file",)

#: Names known at import time. Call :func:`available_sources` for the live list
#: once plugins have been discovered.
SOURCE_NAMES: Sequence[str] = tuple(SOURCE_CLASSES) + BUILTIN_EXTRA

_plugins_loaded = False


def register_source(source_class: type) -> type:
    """Register a :class:`Source` subclass under its ``name``.

    Usable as a decorator, and the hook third-party packages call to add feeds
    without modifying this module.
    """
    if not isinstance(source_class, type) or not issubclass(source_class, Source):
        raise TypeError(f"{source_class!r} is not a Source subclass")
    name = getattr(source_class, "name", "")
    if not name or name in BUILTIN_EXTRA:
        raise ValueError("a source needs a unique, non-empty name")
    SOURCE_CLASSES[name] = source_class
    return source_class


def load_plugin_sources() -> List[str]:
    """Import sources advertised on the ``domain_atlas.sources`` entry point.

    Returns the names that were added. Import failures are reported but never
    stop the application from starting.
    """
    global _plugins_loaded
    if _plugins_loaded:
        return []
    _plugins_loaded = True

    try:
        from importlib.metadata import entry_points
    except ImportError:
        return []

    added: List[str] = []
    try:
        found = entry_points(group="domain_atlas.sources")
    except TypeError:
        found = entry_points().get("domain_atlas.sources", [])
    for entry in found:
        try:
            candidate = entry.load()
            register_source(candidate)
            added.append(candidate.name)
        except Exception as exc:
            logging.getLogger("domain-atlas").warning(
                "could not load source plugin %s: %s", getattr(entry, "name", entry), exc
            )
    return added


def available_sources() -> Sequence[str]:
    load_plugin_sources()
    return tuple(SOURCE_CLASSES) + BUILTIN_EXTRA


def build_sources(config) -> List[Source]:
    """Instantiate the sources named in *config*. Raises ``ValueError`` on typos."""
    load_plugin_sources()
    sources: List[Source] = []
    kwargs = dict(user_agent=config.user_agent, cache_dir=config.cache_dir, retries=config.source_retries)
    for name in config.sources:
        if name == "file":
            if not config.seed_file:
                raise ValueError("source 'file' requires seed_file to be set")
            sources.append(SeedFileSource(config.seed_file, **kwargs))
            continue
        cls = SOURCE_CLASSES.get(name)
        if cls is None:
            raise ValueError(
                f"unknown source {name!r}; valid sources: {', '.join(available_sources())}"
            )
        if cls is OnionIndexSource:
            sources.append(OnionIndexSource(url=config.onion_index_url, **kwargs))
            continue
        if cls is CertStreamSource:
            sources.append(
                CertStreamSource(
                    url=config.certstream_url,
                    buffer_size=config.certstream_buffer,
                    wait=config.certstream_wait,
                    **kwargs,
                )
            )
            continue
        sources.append(cls(**kwargs))
    if config.seed_file and not any(isinstance(s, SeedFileSource) for s in sources):
        sources.insert(0, SeedFileSource(config.seed_file, **kwargs))
    if not sources:
        raise ValueError("no sources configured")
    return sources
