"""Asynchronous collection engine.

A producer pulls candidates from the configured sources, tracking per-source
failures and cooldowns. Consumers probe each domain over HTTPS then HTTP and
fingerprint the response. Progress is published as events for any front-end to
consume. Shutdown cancels every task and closes every session and the database.
"""

from __future__ import annotations

import asyncio
import re
import ssl
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import aiohttp

from . import tech as tech_module
from .config import Config
from .ctlog import dns_names
from .domains import is_onion, normalize_domain
from .httputil import read_capped
from .sources import Source, SourceEmpty, SourceError, build_sources
from .store import DomainRecord, DomainStore

EventCallback = Callable[["Event"], None]


@dataclass
class Event:
    """Something worth telling the front-end about."""

    kind: str  # "log" | "domain" | "stats" | "state" | "cycle"
    message: str = ""
    level: str = "INFO"  # INFO | GOOD | WARN | ERROR
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Stats:
    processed: int = 0
    responsive: int = 0
    new: int = 0
    duplicates: int = 0
    rechecked: int = 0
    #: Host names harvested from probed pages and certificates.
    discovered: int = 0
    unreachable: int = 0
    errors: int = 0
    cycles: int = 0
    queued: int = 0
    started_at: float = field(default_factory=time.time)
    tech_counts: Counter = field(default_factory=Counter)
    source_ok: Counter = field(default_factory=Counter)
    source_fail: Counter = field(default_factory=Counter)

    @property
    def elapsed(self) -> float:
        return max(0.0, time.time() - self.started_at)

    @property
    def rate(self) -> float:
        """Domains probed per second."""
        elapsed = self.elapsed
        return self.processed / elapsed if elapsed > 0 else 0.0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "processed": self.processed,
            "responsive": self.responsive,
            "new": self.new,
            "duplicates": self.duplicates,
            "rechecked": self.rechecked,
            "discovered": self.discovered,
            "unreachable": self.unreachable,
            "errors": self.errors,
            "cycles": self.cycles,
            "queued": self.queued,
            "elapsed": self.elapsed,
            "rate": self.rate,
            "tech_counts": dict(self.tech_counts),
        }


@dataclass
class ProbeResult:
    responsive: bool = False
    reached: bool = False
    status_code: Optional[int] = None
    scheme: Optional[str] = None
    technologies: List[str] = field(default_factory=list)
    versions: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None
    elapsed_ms: int = 0
    #: Host names seen in the page and certificate, for self-expansion.
    discovered: List[str] = field(default_factory=list)


class TorUnavailable(RuntimeError):
    """Tor was requested but cannot be used; the message says why."""


def tor_connector(config: Config):
    """A SOCKS connector for reaching .onion services.

    Raises :class:`TorUnavailable` with a specific reason rather than failing
    silently: without it, .onion domains are still discovered and stored, they
    are simply not probed. Must be called from inside a running event loop.
    """
    if not config.tor_proxy:
        raise TorUnavailable("no tor_proxy configured")
    try:
        from aiohttp_socks import ProxyConnector
    except ImportError as exc:
        raise TorUnavailable(
            "aiohttp-socks is not installed (pip install aiohttp-socks)"
        ) from exc
    try:
        return ProxyConnector.from_url(config.tor_proxy, limit=max(4, config.concurrency))
    except Exception as exc:
        raise TorUnavailable(f"{config.tor_proxy}: {type(exc).__name__}: {exc}") from exc


def _ssl_argument(config: Config):
    """aiohttp ``ssl`` argument: verified, or a permissive context."""
    if config.verify_ssl:
        return None  # aiohttp default: verify
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        context.set_ciphers("DEFAULT@SECLEVEL=1")
    except ssl.SSLError:  # pragma: no cover - platform dependent
        pass
    return context


async def _read_body(response: "aiohttp.ClientResponse", max_bytes: int) -> str:
    """Read at most *max_bytes* of the body and decode it defensively.

    ``ClientResponse.text()`` has no size limit (the 1.x code passed a
    ``limit=`` keyword that does not exist and raised ``TypeError`` on every
    single domain), so the body is read straight off the stream instead.
    """
    raw = await read_capped(response, max_bytes)
    if not raw:
        return ""
    encoding = "utf-8"
    try:
        encoding = response.get_encoding() or "utf-8"
    except (RuntimeError, LookupError, ValueError):
        encoding = "utf-8"
    try:
        return raw.decode(encoding, errors="ignore")
    except LookupError:
        return raw.decode("utf-8", errors="ignore")


#: Host names appearing in href/src attributes and bare URLs.
_LINK_RE = re.compile(r"(?:https?:)?//([A-Za-z0-9._~-]{4,253})(?=[/\"'?:#\s>]|$)")

#: Hosts that appear on nearly every page and add nothing to an inventory.
_LINK_NOISE = frozenset({
    "www.w3.org", "schema.org", "www.googletagmanager.com", "fonts.googleapis.com",
    "fonts.gstatic.com", "www.google-analytics.com", "cdn.jsdelivr.net",
    "cdnjs.cloudflare.com", "unpkg.com", "ajax.googleapis.com", "gmpg.org",
})


def hosts_in_html(html: str, limit: int = 60) -> List[str]:
    """Host names linked from a page, for feeding discovery back on itself."""
    if not html:
        return []
    found: List[str] = []
    seen = set()
    for match in _LINK_RE.finditer(html):
        host = match.group(1).lower().rstrip(".")
        if host in seen or host in _LINK_NOISE:
            continue
        seen.add(host)
        found.append(host)
        if len(found) >= limit:
            break
    return found


def hosts_in_certificate(ssl_object) -> List[str]:
    """Host names in the peer certificate of an open TLS connection.

    The certificate is read in DER form so it works even when verification is
    disabled, which is the default for reachability probing.
    """
    if ssl_object is None:
        return []
    try:
        der = ssl_object.getpeercert(binary_form=True)
    except (ValueError, AttributeError, OSError):
        return []
    if not der:
        return []
    try:
        return dns_names(der)
    except Exception:
        return []


def _peer_ssl_object(response: "aiohttp.ClientResponse"):
    connection = getattr(response, "connection", None)
    transport = getattr(connection, "transport", None)
    if transport is None:
        return None
    try:
        return transport.get_extra_info("ssl_object")
    except Exception:
        return None


def _clean_error(exc: BaseException) -> str:
    """Short, readable one-line error text (aiohttp bakes SSLContext reprs in)."""
    message = str(exc).strip()
    message = re.sub(r"\s*ssl:\s*(?:<[^>]*>|\S+)", "", message)
    message = re.sub(r"\s+", " ", message)
    if len(message) > 160:
        message = message[:157] + "..."
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


async def probe_domain(
    session: "aiohttp.ClientSession",
    domain: str,
    config: Config,
    ssl_arg: Any = None,
) -> ProbeResult:
    """Try HTTPS then HTTP; fingerprint the first successful response."""
    started = time.perf_counter()
    last_error: Optional[str] = None
    last_status: Optional[int] = None
    timeout = aiohttp.ClientTimeout(total=config.http_timeout)

    best: Optional[ProbeResult] = None
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}"
        try:
            async with session.get(url, timeout=timeout, allow_redirects=True, ssl=ssl_arg) as response:
                last_status = response.status
                body = await _read_body(response, config.max_body_bytes)
                discovered: List[str] = []
                if config.expand_from_certificates and scheme == "https":
                    discovered.extend(hosts_in_certificate(_peer_ssl_object(response)))
                if config.expand_from_html:
                    discovered.extend(hosts_in_html(body))

                versions = tech_module.detect_versions(response.headers, body, url=str(response.url))
                if config.use_builtwith:
                    extra = await asyncio.to_thread(
                        tech_module.detect_with_builtwith, url, dict(response.headers), body
                    )
                    for name in extra:
                        versions.setdefault(name, "")
                result = ProbeResult(
                    responsive=response.status < 400,
                    reached=True,
                    status_code=response.status,
                    scheme=scheme,
                    technologies=sorted(versions),
                    versions=versions,
                    discovered=discovered,
                    error=None if response.status < 400 else f"HTTP {response.status}",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
                if result.responsive:
                    return result
                # Keep the 4xx/5xx result: the server answered, so its headers
                # are still worth fingerprinting if the other scheme fails too.
                best = best or result
                last_error = result.error
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            last_error = "timeout"
        except (aiohttp.ClientError, ssl.SSLError, OSError, UnicodeError, ValueError) as exc:
            last_error = _clean_error(exc)

    if best is not None:
        return best
    return ProbeResult(
        responsive=False,
        reached=False,
        status_code=last_status,
        error=last_error or "unreachable",
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


class Collector:
    """Runs the producer/consumer pipeline until stopped."""

    def __init__(
        self,
        config: Config,
        store: Optional[DomainStore] = None,
        on_event: Optional[EventCallback] = None,
        sources: Optional[Sequence[Source]] = None,
    ) -> None:
        self.config = config
        self.store = store or DomainStore(config.db_path, config.output_dir, config.write_tech_files)
        self._owns_store = store is None
        self._on_event = on_event
        if sources is not None:
            self.sources: List[Source] = list(sources)
        else:
            # build_sources reads this to wire the self-expansion source.
            setattr(config, "_store", self.store)
            self.sources = build_sources(config)
        self.stats = Stats()

        self._queue: Optional[asyncio.Queue] = None
        # Events are created inside the running loop: an ``asyncio.Event``
        # built on one loop cannot be awaited on another (the GUI runs the
        # engine in a worker thread with its own loop).
        self._stop: Optional[asyncio.Event] = None
        self._resume: Optional[asyncio.Event] = None
        self._stop_requested = False
        self._pause_requested = False
        self._tasks: List[asyncio.Task] = []
        self._cooldowns: Dict[str, float] = {}
        self._tor_session: Optional["aiohttp.ClientSession"] = None
        #: Domains queued for a re-check; they are already in the store, so
        #: store.reserve() cannot be used to keep them out of the queue twice.
        self._rechecking: set = set()
        self._running = False

    # ------------------------------------------------------------------ state
    def _init_events(self) -> None:
        self._stop = asyncio.Event()
        self._resume = asyncio.Event()
        if self._stop_requested:
            self._stop.set()
        if self._pause_requested:
            self._resume.clear()
        else:
            self._resume.set()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._pause_requested

    def pause(self) -> None:
        if self._pause_requested:
            return
        self._pause_requested = True
        if self._resume is not None:
            self._resume.clear()
        self._emit(Event("state", "Paused.", "WARN", {"state": "paused"}))

    def resume(self) -> None:
        if not self._pause_requested:
            return
        self._pause_requested = False
        if self._resume is not None:
            self._resume.set()
        self._emit(Event("state", "Resumed.", "INFO", {"state": "running"}))

    def stop(self) -> None:
        self._stop_requested = True
        self._pause_requested = False
        if self._stop is not None:
            self._stop.set()
        if self._resume is not None:
            self._resume.set()  # let paused workers observe the stop flag

    # ----------------------------------------------------------------- events
    def _emit(self, event: Event) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            # A front-end error must not stop collection.
            pass

    def _log(self, message: str, level: str = "INFO", **data: Any) -> None:
        self._emit(Event("log", message, level, data))

    # -------------------------------------------------------------------- run
    async def run(self, cycles: Optional[int] = None) -> Stats:
        """Run until :meth:`stop` is called, or for *cycles* fetch cycles."""
        self._running = True
        self._stop_requested = False
        self._init_events()
        self.stats = Stats()
        connector = aiohttp.TCPConnector(
            limit=self.config.concurrency * 2,
            limit_per_host=4,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
            force_close=True,
        )
        session = aiohttp.ClientSession(
            headers={"User-Agent": self.config.user_agent, "Accept": "*/*"},
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=self.config.http_timeout),
            trust_env=True,  # honour HTTP(S)_PROXY / NO_PROXY
        )
        if self._owns_store:
            await self.store.open()
        self._queue = asyncio.Queue(maxsize=self.config.max_queue_size)
        self._emit(Event("state", "Started.", "INFO", {"state": "running"}))
        if self.config.recheck_only:
            self._log(
                f"Collector started - {self.config.concurrency} workers, "
                f"re-check only (domains older than {self.config.recheck_after}s)"
            )
        else:
            self._log(
                f"Collector started - {self.config.concurrency} workers, "
                f"sources: {', '.join(source.name for source in self.sources)}"
                + (f", re-checking after {self.config.recheck_after}s"
                   if self.config.recheck_after else "")
            )

        tor_session: Optional[aiohttp.ClientSession] = None
        if self.config.tor_proxy:
            try:
                tor_session = aiohttp.ClientSession(
                    headers={"User-Agent": self.config.user_agent},
                    connector=tor_connector(self.config),
                    timeout=aiohttp.ClientTimeout(total=max(30.0, self.config.http_timeout)),
                )
                self._log(f"Tor enabled for .onion domains via {self.config.tor_proxy}")
            except TorUnavailable as exc:
                self._log(
                    f"Tor unavailable ({exc}); .onion domains will be stored "
                    "but not probed",
                    "WARN",
                )
        self._tor_session = tor_session

        producer = asyncio.create_task(self._producer(session, cycles), name="producer")
        consumers = [
            asyncio.create_task(self._consumer(session, index), name=f"consumer-{index}")
            for index in range(self.config.concurrency)
        ]
        self._tasks = [producer] + consumers
        try:
            await producer
            if cycles is not None:
                await self._drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - safety net
            self._log(f"Fatal collector error: {exc}", "ERROR")
        finally:
            self.stop()
            for task in consumers:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []
            for source in self.sources:
                try:
                    await source.close()
                except Exception:
                    pass
            try:
                if self._owns_store:
                    await self.store.close()
                else:
                    await self.store.flush()
            finally:
                if tor_session is not None:
                    await tor_session.close()
                self._tor_session = None
                await session.close()
                await connector.close()
                # let aiohttp close its transports before the loop goes away
                await asyncio.sleep(0)
            self._running = False
            self._emit(Event("state", "Stopped.", "INFO", {"state": "stopped"}))
            self._emit(Event("stats", data=self.stats.snapshot()))
        return self.stats

    async def _drain(self) -> None:
        """Wait for the queue to empty (used by ``--once``)."""
        assert self._queue is not None
        while not self._stop.is_set() and not self._queue.empty():
            await asyncio.sleep(0.2)
        if not self._stop.is_set():
            try:
                await asyncio.wait_for(self._queue.join(), timeout=self.config.http_timeout * 3)
            except asyncio.TimeoutError:
                pass

    # --------------------------------------------------------------- producer
    async def _producer(self, session: "aiohttp.ClientSession", cycles: Optional[int]) -> None:
        assert self._queue is not None
        cycle = 0
        while not self._stop.is_set():
            await self._resume.wait()
            if self._stop.is_set():
                break
            cycle += 1
            self.stats.cycles = cycle
            queued = await self._fetch_cycle(session)
            self._emit(Event("cycle", f"Cycle {cycle}: queued {queued} domains", "INFO",
                             {"cycle": cycle, "queued": queued}))
            self._emit(Event("stats", data=self.stats.snapshot()))
            if cycles is not None and cycle >= cycles:
                break
            await self._sleep_interruptible(self.config.fetch_interval)

    async def _fetch_cycle(self, session: "aiohttp.ClientSession") -> int:
        assert self._queue is not None
        wanted = self.config.max_domains_per_cycle
        # Re-checks have their own budget (recheck_batch) and must not consume
        # the discovery budget, or a large recheck_batch stops discovery.
        rechecked = await self._queue_rechecks()
        queued = 0
        if self.config.recheck_only:
            self.stats.queued = self._queue.qsize()
            return rechecked
        now = time.monotonic()
        usable = [s for s in self.sources if self._cooldowns.get(s.name, 0.0) <= now]
        if not usable:
            soonest = min(self._cooldowns.values()) - now
            self._log(f"All sources cooling down, retrying in {max(1, int(soonest))}s", "WARN")
            return rechecked

        # Each source gets an equal share of the cycle budget. Serving them in
        # order until the budget ran out let the first source starve the rest.
        share = max(1, wanted // len(usable))
        for index, source in enumerate(usable):
            if self._stop.is_set() or queued >= wanted:
                break
            remaining = wanted - queued
            # The last source may use whatever the others left unclaimed.
            allowance = remaining if index == len(usable) - 1 else min(share, remaining)
            try:
                candidates = await source.fetch(session, allowance)
                self.stats.source_ok[source.name] += 1
                self._cooldowns.pop(source.name, None)
            except asyncio.CancelledError:
                raise
            except SourceEmpty as exc:
                # Healthy, just nothing queued yet; no cooldown.
                self._log(f"{source.name}: {exc}", "INFO")
                continue
            except SourceError as exc:
                self.stats.source_fail[source.name] += 1
                cooldown = min(900, 60 * self.stats.source_fail[source.name])
                self._cooldowns[source.name] = time.monotonic() + cooldown
                self._log(f"Source {source.name} unavailable ({exc}); pausing it for {cooldown}s", "WARN")
                continue
            except Exception as exc:  # pragma: no cover - unexpected source bug
                self.stats.source_fail[source.name] += 1
                self._cooldowns[source.name] = time.monotonic() + 300
                self._log(f"Source {source.name} raised {type(exc).__name__}: {exc}", "ERROR")
                continue

            normalised: List[str] = []
            for candidate in candidates:
                fingerprint = normalize_domain(candidate)
                if fingerprint:
                    normalised.append(fingerprint)

            fresh = await self.store.filter_new(normalised)
            self.stats.duplicates += len(normalised) - len(fresh)

            if not self.config.probe_domains:
                added = await self._record_unprobed(fresh, source.name)
                queued += added
                self._log(f"{source.name}: {added} new domains recorded",
                          "GOOD" if added else "INFO")
                continue

            added = 0
            for fingerprint in fresh:
                if self._stop.is_set():
                    break
                if not self.store.reserve(fingerprint):
                    self.stats.duplicates += 1
                    continue
                if not await self._enqueue(fingerprint, source.name):
                    self.store.release(fingerprint)
                    break
                added += 1
            queued += added
            self._log(f"{source.name}: {added} new domains queued", "GOOD" if added else "INFO")

        self.stats.queued = self._queue.qsize()
        return queued + rechecked

    async def _record_unprobed(self, fingerprints: Sequence[str], source_name: str) -> int:
        """Store names without connecting to them.

        Probing is the slow half of a cycle: a name costs one HTTP request and
        its timeout, so the collector moves at a few names a second. Recording
        alone is bounded by how fast the sources produce names, which is
        thousands a second. The rows land with no status, no technologies and
        a null checked_at, so enabling probing later picks them up as if they
        had never been checked.
        """
        stored = 0
        for fingerprint in fingerprints:
            if self._stop.is_set():
                break
            if not self.store.reserve(fingerprint):
                self.stats.duplicates += 1
                continue
            record = DomainRecord(
                fingerprint=fingerprint,
                responsive=False,
                source=source_name,
                probed=False,
            )
            if await self.store.add(record):
                stored += 1
                self.stats.new += 1
            else:
                self.stats.duplicates += 1
                self.store.release(fingerprint)
        self.stats.processed += stored
        return stored

    async def _queue_rechecks(self) -> int:
        """Re-queue domains whose last check is older than ``recheck_after``."""
        if self.config.recheck_after <= 0:
            return 0
        try:
            stale = await self.store.stale_domains(self.config.recheck_after, self.config.recheck_batch)
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"Could not look up stale domains: {type(exc).__name__}: {exc}", "ERROR")
            return 0
        queued = 0
        for fingerprint in stale:
            if self._stop is not None and self._stop.is_set():
                break
            if fingerprint in self._rechecking:
                continue
            self._rechecking.add(fingerprint)
            if not await self._enqueue(fingerprint, "recheck", recheck=True):
                self._rechecking.discard(fingerprint)
                break
            queued += 1
        if queued:
            age = self.config.recheck_after
            self._log(f"recheck: {queued} domains re-queued (older than {age}s)", "INFO")
        return queued

    async def _enqueue(self, fingerprint: str, source_name: str, recheck: bool = False) -> bool:
        """Put a domain on the queue, giving up if the queue stays full."""
        assert self._queue is not None
        while not self._stop.is_set():
            try:
                self._queue.put_nowait((fingerprint, source_name, recheck))
                return True
            except asyncio.QueueFull:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                return False
        return False

    async def _record_discoveries(self, origin: str, hosts: Sequence[str]) -> None:
        """Queue host names harvested from a probe for a later cycle."""
        candidates = []
        for host in hosts:
            fingerprint = normalize_domain(host)
            if fingerprint and fingerprint != origin:
                candidates.append(fingerprint)
        if not candidates:
            return
        try:
            added = await self.store.push_frontier(candidates, origin=origin)
        except Exception:
            return
        if added:
            self.stats.discovered += added

    async def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep, but wake immediately when the collector is stopped."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # --------------------------------------------------------------- consumer
    async def _consumer(self, session: "aiohttp.ClientSession", worker_id: int) -> None:
        assert self._queue is not None
        ssl_arg = _ssl_argument(self.config)
        while not self._stop.is_set():
            await self._resume.wait()
            if self._stop.is_set():
                break
            try:
                fingerprint, source_name, is_recheck = await asyncio.wait_for(
                    self._queue.get(), timeout=0.5
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                if is_onion(fingerprint):
                    if self._tor_session is None:
                        result = ProbeResult(
                            responsive=False,
                            reached=False,
                            error="not probed: no Tor proxy configured",
                        )
                    else:
                        result = await probe_domain(
                            self._tor_session, fingerprint, self.config, None
                        )
                else:
                    result = await probe_domain(session, fingerprint, self.config, ssl_arg)
                if not result.responsive and not self.config.store_unresponsive and not is_recheck:
                    self.store.release(fingerprint)
                else:
                    record = DomainRecord(
                        fingerprint=fingerprint,
                        responsive=result.responsive,
                        technologies=result.technologies,
                        versions=result.versions,
                        status_code=result.status_code,
                        scheme=result.scheme,
                        error=result.error,
                        elapsed_ms=result.elapsed_ms,
                        source=source_name,
                        recheck=is_recheck,
                    )
                    if await self.store.add(record):
                        if is_recheck:
                            self.stats.rechecked += 1
                        else:
                            self.stats.new += 1
                    else:
                        self.stats.duplicates += 1
                if result.discovered:
                    await self._record_discoveries(fingerprint, result.discovered)
                self.stats.processed += 1
                if result.responsive:
                    self.stats.responsive += 1
                else:
                    self.stats.unreachable += 1
                for technology in result.technologies:
                    self.stats.tech_counts[technology] += 1
                verdict = "OK" if result.responsive else "DOWN"
                details = ["re-check"] if is_recheck else []
                if result.status_code is not None:
                    details.append(f"HTTP {result.status_code}")
                elif result.error:
                    details.append(result.error)
                if result.technologies:
                    details.append(", ".join(
                        f"{name} {result.versions.get(name, '')}".strip() for name in result.technologies
                    ))
                elif result.responsive:
                    details.append("no technologies detected")
                self._emit(
                    Event(
                        "domain",
                        f"{fingerprint} -> {verdict} [{'] ['.join(details)}]" if details
                        else f"{fingerprint} -> {verdict}",
                        "GOOD" if result.responsive else "WARN",
                        {
                            "domain": fingerprint,
                            "responsive": result.responsive,
                            "technologies": result.technologies,
                            "status": result.status_code,
                            "source": source_name,
                            "recheck": is_recheck,
                        },
                    )
                )
            except asyncio.CancelledError:
                self.store.release(fingerprint)
                raise
            except Exception as exc:  # pragma: no cover - defensive
                self.stats.errors += 1
                self.store.release(fingerprint)
                self._log(f"Error processing {fingerprint}: {type(exc).__name__}: {exc}", "ERROR")
            finally:
                self._rechecking.discard(fingerprint)
                self._queue.task_done()
                self.stats.queued = self._queue.qsize()
