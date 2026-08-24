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
from .engine import Collector, Event
from .sources import SOURCE_NAMES
from .store import DomainStore

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
DATE_FORMAT = "%H:%M:%S"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domain_collector",
        description="Discover live domains and fingerprint the technologies they run on.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"domain-collector {__version__}")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH, help="path to the JSON config file")
    parser.add_argument("--headless", "--no-gui", dest="headless", action="store_true",
                        help="run in the terminal instead of opening the GUI")
    parser.add_argument("--gui", dest="gui", action="store_true", help="force the GUI")
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
                        help=f"comma separated feed list ({', '.join(SOURCE_NAMES)})")
    parser.add_argument("--seed-file", default=None, dest="seed_file",
                        help="file with candidate domains, one per line")
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
    parser.add_argument("--export", metavar="TECHNOLOGY",
                        help="print the stored domains for a technology and exit")
    return parser


def apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    for field_name in ("concurrency", "http_timeout", "fetch_interval", "max_domains_per_cycle",
                       "db_path", "output_dir", "seed_file", "log_level"):
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
    return config.validate()


def _make_logger(level: str) -> logging.Logger:
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format=LOG_FORMAT, datefmt=DATE_FORMAT, stream=sys.stdout)
    return logging.getLogger("domain-collector")


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
    logger.info(
        "Finished: %d probed, %d responsive, %d unreachable, %d new, %d errors "
        "in %d cycle(s) (%.1f/s)",
        stats.processed, stats.responsive, stats.unreachable, stats.new,
        stats.errors, stats.cycles, stats.rate,
    )
    if stats.tech_counts:
        logger.info("Top technologies: %s", ", ".join(
            f"{name} ({count})" for name, count in stats.tech_counts.most_common(10)))
    return 0


async def run_stats(config: Config) -> int:
    store = DomainStore(config.db_path, config.output_dir, write_tech_files=False)
    if not os.path.exists(config.db_path):
        print(f"No database at {config.db_path} yet.")
        return 1
    await store.open()
    try:
        summary = await store.summary()
        print(f"Database        : {config.db_path}")
        print(f"Domains stored  : {summary['total']}")
        print(f"Responsive      : {summary['responsive']}")
        print(f"Technologies    : {summary['technologies']}")
        top = await store.top_technologies(20)
        if top:
            print("\nTop technologies")
            width = max(len(name) for name, _ in top)
            for name, count in top:
                print(f"  {name.ljust(width)}  {count}")
    finally:
        await store.close()
    return 0


async def run_export(config: Config, technology: str) -> int:
    if not os.path.exists(config.db_path):
        print(f"No database at {config.db_path} yet.", file=sys.stderr)
        return 1
    store = DomainStore(config.db_path, config.output_dir, write_tech_files=False)
    await store.open()
    try:
        domains = await store.domains_for_technology(technology, limit=1_000_000)
        for domain in domains:
            print(domain)
        if not domains:
            print(f"No domains stored for technology {technology!r}.", file=sys.stderr)
            return 1
    finally:
        await store.close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = Config.load(args.config)
        config = apply_overrides(config, args)
    except ConfigError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover - argparse exits

    if args.save_config:
        try:
            config.save(args.config)
        except ConfigError as exc:
            print(f"Could not save config: {exc}", file=sys.stderr)
            return 1
        print(f"Configuration written to {args.config}")
        return 0

    if args.stats:
        return asyncio.run(run_stats(config))
    if args.export:
        return asyncio.run(run_export(config, args.export))

    cycles = 1 if args.once else args.cycles
    if cycles is not None and cycles < 1:
        parser.error("--cycles must be at least 1")

    want_gui = args.gui or not args.headless
    if want_gui:
        from .gui import run_gui, tkinter_available

        if tkinter_available():
            if cycles is not None:
                print("--once/--cycles only apply to headless mode; ignoring.", file=sys.stderr)
            try:
                return run_gui(config, args.config)
            except Exception as exc:  # pragma: no cover - display problems
                print(f"GUI could not start ({exc}); falling back to headless mode.", file=sys.stderr)
        elif args.gui:
            from .gui import run_gui

            try:
                run_gui(config, args.config)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        else:
            print("tkinter not available - running headless. Use --headless to silence this notice.",
                  file=sys.stderr)

    try:
        return asyncio.run(run_headless(config, cycles))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
