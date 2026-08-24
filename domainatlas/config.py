"""Configuration handling for Domain Atlas.

The configuration is a plain dataclass that can be loaded from / saved to a
JSON file.  Every value is validated and clamped to a sane range so a bad
config file can never crash the collector at run time.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG_PATH = "config.json"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 domain-atlas/2.0"
)

#: Public certstream server.  It is frequently idle - point this at your own
#: certstream-server-go instance for a reliable feed.
DEFAULT_CERTSTREAM_URL = "wss://certstream.calidog.io/domains-only"

#: Sources enabled when the user does not choose explicitly.
DEFAULT_SOURCES: List[str] = ["crtsh", "tranco", "umbrella"]

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(ValueError):
    """Raised when a configuration value cannot be used."""


def _as_int(name: str, value: Any, minimum: int, maximum: int, default: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a whole number, got {value!r}") from None
    if number < minimum or number > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {number}")
    return number


def _as_float(name: str, value: Any, minimum: float, maximum: float, default: float) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a number, got {value!r}") from None
    if number < minimum or number > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {number}")
    return number


def _as_bool(name: str, value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    raise ConfigError(f"{name} must be true or false, got {value!r}")


@dataclass
class Config:
    """Runtime settings.  Instances are validated by :meth:`validate`."""

    concurrency: int = 20
    http_timeout: float = 10.0
    fetch_interval: int = 1800
    max_queue_size: int = 10000
    max_domains_per_cycle: int = 500
    max_body_bytes: int = 262144
    source_retries: int = 3
    #: Re-probe a stored domain once it is older than this many seconds
    #: (0 disables re-checking entirely).  86400 = once a day.
    recheck_after: int = 0
    #: How many stale domains to re-queue per cycle.
    recheck_batch: int = 100
    #: Only re-check stored domains; do not pull new candidates from sources.
    recheck_only: bool = False
    #: Live Certificate Transparency stream (certstream-server protocol).
    certstream_url: str = DEFAULT_CERTSTREAM_URL
    #: How many streamed domains to hold between cycles.
    certstream_buffer: int = 20000
    #: Seconds a certstream fetch waits for the stream to produce something.
    certstream_wait: float = 15.0
    #: SOCKS5 proxy used to reach .onion services, e.g. socks5://127.0.0.1:9050.
    #: Empty means Tor is disabled: .onion domains are still discovered and
    #: stored, but not probed.
    tor_proxy: str = ""
    #: Public index used by the "onion" source.
    onion_index_url: str = "https://ahmia.fi/onions/"
    db_path: str = "domains.db"
    output_dir: str = "output"
    cache_dir: str = ".cache"
    user_agent: str = DEFAULT_USER_AGENT
    sources: List[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    seed_file: Optional[str] = None
    verify_ssl: bool = False
    write_tech_files: bool = True
    store_unresponsive: bool = True
    use_builtwith: bool = False
    log_level: str = "INFO"

    # ---------------------------------------------------------------- helpers
    def validate(self) -> "Config":
        """Normalise and range-check every field.  Returns ``self``."""
        defaults = Config()
        self.concurrency = _as_int("concurrency", self.concurrency, 1, 500, defaults.concurrency)
        self.http_timeout = _as_float("http_timeout", self.http_timeout, 1.0, 300.0, defaults.http_timeout)
        self.fetch_interval = _as_int("fetch_interval", self.fetch_interval, 10, 86400, defaults.fetch_interval)
        self.max_queue_size = _as_int("max_queue_size", self.max_queue_size, 10, 1_000_000, defaults.max_queue_size)
        self.max_domains_per_cycle = _as_int(
            "max_domains_per_cycle", self.max_domains_per_cycle, 1, 1_000_000, defaults.max_domains_per_cycle
        )
        self.max_body_bytes = _as_int("max_body_bytes", self.max_body_bytes, 1024, 20_971_520, defaults.max_body_bytes)
        self.source_retries = _as_int("source_retries", self.source_retries, 1, 10, defaults.source_retries)
        self.recheck_after = _as_int("recheck_after", self.recheck_after, 0, 31_536_000, defaults.recheck_after)
        self.recheck_batch = _as_int("recheck_batch", self.recheck_batch, 1, 100_000, defaults.recheck_batch)
        self.certstream_buffer = _as_int(
            "certstream_buffer", self.certstream_buffer, 100, 1_000_000, defaults.certstream_buffer
        )
        self.certstream_wait = _as_float(
            "certstream_wait", self.certstream_wait, 1.0, 300.0, defaults.certstream_wait
        )
        tor_proxy = str(self.tor_proxy or "").strip()
        if tor_proxy and not tor_proxy.startswith(("socks5://", "socks5h://", "socks4://")):
            raise ConfigError(
                f"tor_proxy must be a socks5:// URL, got {tor_proxy!r}"
            )
        self.tor_proxy = tor_proxy
        onion_index = str(self.onion_index_url or "").strip() or defaults.onion_index_url
        if not onion_index.startswith(("http://", "https://")):
            raise ConfigError(f"onion_index_url must be an http(s) URL, got {onion_index!r}")
        self.onion_index_url = onion_index

        certstream_url = str(self.certstream_url or "").strip() or defaults.certstream_url
        if not certstream_url.startswith(("ws://", "wss://")):
            raise ConfigError(f"certstream_url must start with ws:// or wss://, got {certstream_url!r}")
        self.certstream_url = certstream_url
        self.verify_ssl = _as_bool("verify_ssl", self.verify_ssl, defaults.verify_ssl)
        self.write_tech_files = _as_bool("write_tech_files", self.write_tech_files, defaults.write_tech_files)
        self.store_unresponsive = _as_bool("store_unresponsive", self.store_unresponsive, defaults.store_unresponsive)
        self.use_builtwith = _as_bool("use_builtwith", self.use_builtwith, defaults.use_builtwith)
        self.recheck_only = _as_bool("recheck_only", self.recheck_only, defaults.recheck_only)
        if self.recheck_only and self.recheck_after <= 0:
            raise ConfigError("recheck_only needs recheck_after to be greater than 0")

        if not str(self.db_path).strip():
            raise ConfigError("db_path must not be empty")
        if not str(self.output_dir).strip():
            raise ConfigError("output_dir must not be empty")
        self.db_path = str(self.db_path).strip()
        self.output_dir = str(self.output_dir).strip()
        self.cache_dir = str(self.cache_dir).strip() or ".cache"

        if not str(self.user_agent).strip():
            self.user_agent = defaults.user_agent

        if isinstance(self.sources, str):
            self.sources = [part.strip() for part in self.sources.split(",")]
        if not isinstance(self.sources, (list, tuple)):
            raise ConfigError("sources must be a list of source names")
        cleaned = [str(name).strip().lower() for name in self.sources if str(name).strip()]
        # preserve order, drop duplicates
        self.sources = list(dict.fromkeys(cleaned)) or list(DEFAULT_SOURCES)

        if self.seed_file is not None:
            self.seed_file = str(self.seed_file).strip() or None

        level = str(self.log_level).strip().upper()
        if level not in LOG_LEVELS:
            raise ConfigError(f"log_level must be one of {', '.join(LOG_LEVELS)}")
        self.log_level = level
        return self

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # -------------------------------------------------------------- (de)serial
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"unknown configuration key(s): {', '.join(sorted(unknown))}")
        return cls(**{k: v for k, v in data.items() if k in known}).validate()

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "Config":
        """Load a config file.  A missing file yields the defaults."""
        if not path or not os.path.exists(path):
            return cls().validate()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        return cls.from_dict(data)

    def save(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        """Write the config atomically so an interrupted write cannot corrupt it."""
        self.validate()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_path, path)
        except OSError as exc:
            raise ConfigError(f"cannot write {path}: {exc}") from exc
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
