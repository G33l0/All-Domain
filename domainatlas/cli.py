"""Command line interface.

Supports the GUI (default when tkinter is present) and a fully headless mode
with proper Ctrl+C handling, which is what the README always promised.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from typing import Optional, Sequence

from . import __version__
from .config import DEFAULT_CONFIG_PATH, Config, ConfigError
from .paths import ensure_streams, is_frozen, resolve
from .export import FORMATS as EXPORT_FORMATS
from .export import ExportError, export_to_path, format_for_path
from .query import DomainFilter, DomainQuery, QueryError
from .engine import Collector, Event
from .sources import available_sources

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
DATE_FORMAT = "%H:%M:%S"


def default_config_path(requested: str) -> str:
    """Where settings live: beside the checkout, or in the user's data dir."""
    if requested != DEFAULT_CONFIG_PATH:
        return requested
    if is_frozen():
        return resolve(DEFAULT_CONFIG_PATH)
    return requested


def _theme_names() -> Sequence[str]:
    """Theme keys, resolved lazily so the CLI works without PySide6 installed."""
    try:
        from .qtui.theme import theme_names

        return theme_names()
    except Exception:
        return ("light", "dark", "midnight", "aurora", "amber", "hacker")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domain_atlas",
        description="Discover live domains and fingerprint the technologies they run on.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"domain-atlas {__version__}")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH, help="path to the JSON config file")
    parser.add_argument("--headless", "--no-gui", dest="headless", action="store_true",
                        help="run in the terminal instead of opening the desktop app")
    parser.add_argument("--gui", dest="gui", action="store_true", help="force the desktop app")
    parser.add_argument("--ui", choices=["auto", "qt", "tk"], default="auto",
                        help="which desktop interface to use: the modern Qt/PySide6 app, "
                             "the built-in Tk fallback, or whichever is available")
    parser.add_argument("--theme", default=None, metavar="NAME",
                        help="desktop theme: system, " + ", ".join(_theme_names()))
    parser.add_argument("--once", action="store_true", help="run a single fetch cycle, then exit")
    parser.add_argument("--cycles", type=int, default=None, metavar="N", help="stop after N fetch cycles")
    parser.add_argument("--concurrency", type=int, default=None, help="number of concurrent probes")
    parser.add_argument("--timeout", type=float, default=None, dest="http_timeout",
                        help="per-request timeout in seconds")
    parser.add_argument("--interval", type=int, default=None, dest="fetch_interval",
                        help="seconds between fetch cycles")
    parser.add_argument("--limit", type=int, default=None, dest="max_domains_per_cycle",
                        help="max domains queued per cycle")
    parser.add_argument("--db", default=None, dest="db_path", help="SQLite database path")
    parser.add_argument("--output", default=None, dest="output_dir", help="directory for technology files")
    parser.add_argument("--sources", default=None,
                        help=f"comma separated feed list ({', '.join(available_sources())})")
    parser.add_argument("--seed-file", default=None, dest="seed_file",
                        help="file with candidate domains, one per line")
    parser.add_argument("--certstream-url", default=None, dest="certstream_url",
                        help="certstream websocket URL (use your own certstream-server "
                             "instance; the public one is often idle)")
    parser.add_argument("--tor-proxy", default=None, dest="tor_proxy", metavar="URL",
                        help="SOCKS5 proxy used to probe .onion domains, "
                             "e.g. socks5://127.0.0.1:9050")
    parser.add_argument("--onion-index-url", default=None, dest="onion_index_url",
                        metavar="URL", help="public index used by the onion source")
    parser.add_argument("--recheck-after", type=int, default=None, dest="recheck_after",
                        metavar="SECONDS",
                        help="re-probe stored domains older than this (0 disables re-checking)")
    parser.add_argument("--recheck-batch", type=int, default=None, dest="recheck_batch",
                        metavar="N", help="how many stale domains to re-queue per cycle")
    parser.add_argument("--no-probe", dest="no_probe", action="store_true",
                        help="record every name a source reports without connecting to it: "
                             "far more names per hour, no status or technology data")
    parser.add_argument("--recheck-only", action="store_true",
                        help="only re-check stored domains, do not discover new ones "
                             "(requires --recheck-after)")
    parser.add_argument("--no-tech-files", action="store_true", help="do not write output/<tech>.txt")
    parser.add_argument("--responsive-only", action="store_true",
                        help="store only domains that answered")
    parser.add_argument("--verify-ssl", action="store_true", help="verify TLS certificates")
    parser.add_argument("--use-builtwith", action="store_true",
                        help="also run the optional builtwith package, when installed")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--save-config", action="store_true",
                        help="write the resulting settings back to the config file and exit")
    parser.add_argument("--stats", action="store_true", help="print database statistics and exit")

    export_group = parser.add_argument_group("export")
    export_group.add_argument(
        "--export", metavar="PATH",
        help="export stored domains and exit; use - for standard output")
    export_group.add_argument(
        "--export-format", choices=list(EXPORT_FORMATS), default=None,
        help="export format (default: inferred from the file extension, else csv)")
    export_group.add_argument("--filter-technology", metavar="NAME",
                              help="only domains running this technology")
    export_group.add_argument("--filter-source", metavar="NAME",
                              help="only domains discovered by this source")
    export_group.add_argument("--filter-contains", metavar="TEXT",
                              help="only domains whose name contains this text")
    export_group.add_argument("--filter-since", metavar="TIMESTAMP",
                              help="only domains first seen or checked at or after this "
                                   "timestamp, e.g. 2026-01-31 or '2026-01-31 12:00:00'")
    export_group.add_argument("--filter-live", action="store_true",
                              help="only domains that answered")
    export_group.add_argument("--filter-down", action="store_true",
                              help="only domains that did not answer")
    export_group.add_argument("--filter-onion", action="store_true",
                              help="only .onion domains")
    export_group.add_argument("--filter-clearnet", action="store_true",
                              help="exclude .onion domains")
    export_group.add_argument("--sites-only", action="store_true",
                              help="one row per site instead of one per host name "
                                   "(example.com and www.example.com count once)")
    return parser


def filter_from_args(args: argparse.Namespace) -> DomainFilter:
    """Build a query filter from the --filter-* options."""
    responsive: Optional[bool] = None
    if args.filter_live and args.filter_down:
        raise ValueError("--filter-live and --filter-down are mutually exclusive")
    if args.filter_live:
        responsive = True
    elif args.filter_down:
        responsive = False

    onion: Optional[bool] = None
    if args.filter_onion and args.filter_clearnet:
        raise ValueError("--filter-onion and --filter-clearnet are mutually exclusive")
    if args.filter_onion:
        onion = True
    elif args.filter_clearnet:
        onion = False

    return DomainFilter(
        text=args.filter_contains or "",
        technology=args.filter_technology,
        source=args.filter_source,
        responsive=responsive,
        since=args.filter_since,
        onion=onion,
        sites_only=bool(getattr(args, "sites_only", False)),
    )


def apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    for field_name in ("concurrency", "http_timeout", "fetch_interval", "max_domains_per_cycle",
                       "db_path", "output_dir", "seed_file", "log_level",
                       "certstream_url", "recheck_after", "recheck_batch",
                       "tor_proxy", "onion_index_url"):
        value = getattr(args, field_name, None)
        if value is not None:
            setattr(config, field_name, value)
    if args.sources:
        config.sources = [part.strip() for part in args.sources.split(",") if part.strip()]
    if args.no_tech_files:
        config.write_tech_files = False
    if args.responsive_only:
        config.store_unresponsive = False
    if args.verify_ssl:
        config.verify_ssl = True
    if args.use_builtwith:
        config.use_builtwith = True
    if args.recheck_only:
        config.recheck_only = True
    if args.no_probe:
        config.probe_domains = False
    return config.validate()


def _make_logger(level: str) -> logging.Logger:
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format=LOG_FORMAT, datefmt=DATE_FORMAT, stream=sys.stdout)
    # Debug logging is for diagnosing the collector, not for a transcript of
    # every statement the database and HTTP layers run.
    for noisy in ("aiosqlite", "asyncio", "aiohttp", "websockets", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.INFO)
    return logging.getLogger("domain-atlas")


async def run_headless(config: Config, cycles: Optional[int]) -> int:
    logger = _make_logger(config.log_level)
    level_map = {"INFO": logging.INFO, "GOOD": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR}

    def on_event(event: Event) -> None:
        if event.kind == "stats" or not event.message:
            return
        logger.log(level_map.get(event.level, logging.INFO), event.message)

    collector = Collector(config, on_event=on_event)

    loop = asyncio.get_running_loop()
    stopping = {"count": 0}

    def request_stop() -> None:
        stopping["count"] += 1
        if stopping["count"] == 1:
            logger.warning("Shutdown requested - finishing in-flight probes (Ctrl+C again to force).")
            collector.stop()
        else:
            logger.error("Forced exit.")
            raise SystemExit(130)

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal_number, request_stop)

    stats = await collector.run(cycles=cycles)
    if config.probe_domains:
        logger.info(
            "Finished: %d probed, %d responsive, %d unreachable, %d new, %d re-checked, "
            "%d self-discovered, %d errors in %d cycle(s) (%.1f/s)",
            stats.processed, stats.responsive, stats.unreachable, stats.new,
            stats.rechecked, stats.discovered, stats.errors, stats.cycles, stats.rate,
        )
    else:
        logger.info(
            "Finished: %d recorded (not probed), %d already known, %d errors "
            "in %d cycle(s) (%.1f/s)",
            stats.new, stats.duplicates, stats.errors, stats.cycles, stats.rate,
        )
    if stats.tech_counts:
        logger.info("Top technologies: %s", ", ".join(
            f"{name} ({count})" for name, count in stats.tech_counts.most_common(10)))
    return 0


def run_stats(config: Config) -> int:
    if not os.path.exists(config.db_path):
        print(f"No database at {config.db_path} yet.")
        return 1
    try:
        with DomainQuery(config.db_path) as query:
            summary = query.summary()
            print(f"Database      : {config.db_path}")
            print(f"Domains       : {summary['total']:,}")
            print(f"Responsive    : {summary['responsive']:,}")
            print(f"Technologies  : {summary['technologies']:,}")
            print(f"Tor (.onion)  : {summary['onion']:,}")
            sources = query.sources()
            if sources:
                print(f"Sources       : {', '.join(sources)}")
            top = query.technologies(20)
            if top:
                width = max(len(name) for name, _ in top)
                print("\nTop technologies")
                for name, count in top:
                    print(f"  {name.ljust(width)}  {count:>9,}")
    except QueryError as exc:
        print(f"Cannot read the database: {exc}", file=sys.stderr)
        return 1
    return 0


def run_export(config: Config, destination: str, criteria: DomainFilter,
               export_format: Optional[str]) -> int:
    if not os.path.exists(config.db_path):
        print(f"No database at {config.db_path} yet.", file=sys.stderr)
        return 1
    if export_format is None:
        export_format = "txt" if destination == "-" else format_for_path(destination)
    try:
        written = export_to_path(config.db_path, destination, criteria, export_format)
    except (ExportError, QueryError) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    if destination != "-":
        print(f"Exported {written:,} domains to {destination} ({export_format})")
    if not written:
        print("No domains matched the filter.", file=sys.stderr)
        return 1
    return 0


def _run_desktop(args, config: Config, cycles: Optional[int]) -> Optional[int]:
    """Open a desktop interface.  Returns an exit code, or None to fall back."""
    from .qtui import pyside_available, run_qt_gui

    if cycles is not None:
        print("--once/--cycles only apply to headless mode; ignoring.", file=sys.stderr)

    want = args.ui
    if want in ("auto", "qt") and pyside_available():
        try:
            return run_qt_gui(config, default_config_path(args.config), theme=args.theme)
        except Exception as exc:  # pragma: no cover - display problems
            print(f"Qt interface could not start ({exc}).", file=sys.stderr)
            if want == "qt":
                return 1

    if want == "qt":
        from .qtui import import_error

        print(
            "The Qt interface needs PySide6.  Install it with:  pip install PySide6\n"
            f"(import failed with: {import_error()})",
            file=sys.stderr,
        )
        return 1

    from .gui import run_gui, tkinter_available

    if tkinter_available():
        try:
            return run_gui(config, default_config_path(args.config))
        except Exception as exc:  # pragma: no cover - display problems
            print(f"Tk interface could not start ({exc}).", file=sys.stderr)
            if want == "tk":
                return 1
    elif want == "tk":
        from .gui import run_gui as _run_gui

        try:
            _run_gui(config, default_config_path(args.config))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
        return 1

    print("No desktop toolkit available (install PySide6 for the full app) - "
          "running headless. Use --headless to silence this notice.", file=sys.stderr)
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    # A windowed build has no console: printing to a None stream would crash
    # before anything useful happened.
    ensure_streams()
    try:
        return _main(argv)
    except BrokenPipeError:
        # Standard for a piped command whose reader exits early (`| head`).
        # Redirect stdout so the interpreter's own flush cannot raise again.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 0


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config_path = default_config_path(args.config)
        config = Config.load(config_path)
        config = apply_overrides(config, args)
        config.resolve_paths()
    except ConfigError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover - argparse exits

    if args.theme is not None:
        valid = ("system",) + tuple(_theme_names())
        if args.theme not in valid:
            parser.error(f"--theme must be one of: {', '.join(valid)}")

    known = set(available_sources())
    unknown = [name for name in config.sources if name not in known]
    if unknown:
        parser.error("unknown source(s): %s; valid sources: %s"
                     % (", ".join(unknown), ", ".join(available_sources())))

    if args.save_config:
        try:
            config.save(config_path)
        except ConfigError as exc:
            print(f"Could not save config: {exc}", file=sys.stderr)
            return 1
        print(f"Configuration written to {config_path}")
        return 0

    try:
        criteria = filter_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover - argparse exits

    if args.stats:
        return run_stats(config)
    if args.export:
        return run_export(config, args.export, criteria, args.export_format)

    cycles = 1 if args.once else args.cycles
    if cycles is not None and cycles < 1:
        parser.error("--cycles must be at least 1")

    want_gui = args.gui or not args.headless
    if want_gui:
        exit_code = _run_desktop(args, config, cycles)
        if exit_code is not None:
            return exit_code

    try:
        return asyncio.run(run_headless(config, cycles))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
