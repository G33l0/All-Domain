"""Export stored domains to CSV, JSON, JSON Lines or plain text.

Rows are streamed straight from SQLite, so exporting a filtered slice of a
multi-million row database uses constant memory.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from typing import Callable, Iterable, Optional, TextIO

from .query import DomainFilter, DomainQuery, DomainRow

FORMATS = ("csv", "json", "jsonl", "txt")

CSV_FIELDS = (
    "domain", "responsive", "status_code", "scheme", "source",
    "first_seen", "checked_at", "technologies", "error",
)


class ExportError(RuntimeError):
    """The export could not be written."""


def _flatten(row: DomainRow) -> dict:
    data = row.as_dict()
    data["technologies"] = " ".join(row.technologies)
    return data


def write_rows(
    rows: Iterable[DomainRow],
    handle: TextIO,
    export_format: str = "csv",
    progress: Optional[Callable[[int], None]] = None,
) -> int:
    """Write *rows* to an open text handle. Returns the number written."""
    export_format = (export_format or "csv").lower()
    if export_format not in FORMATS:
        raise ExportError(f"unknown format {export_format!r}; choose from {', '.join(FORMATS)}")

    written = 0
    if export_format == "csv":
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_flatten(row))
            written += 1
            if progress and written % 5000 == 0:
                progress(written)
    elif export_format == "jsonl":
        for row in rows:
            handle.write(json.dumps(row.as_dict(), separators=(",", ":")) + "\n")
            written += 1
            if progress and written % 5000 == 0:
                progress(written)
    elif export_format == "json":
        handle.write("[\n")
        for row in rows:
            if written:
                handle.write(",\n")
            handle.write("  " + json.dumps(row.as_dict(), separators=(",", ":")))
            written += 1
            if progress and written % 5000 == 0:
                progress(written)
        handle.write("\n]\n")
    else:  # txt
        for row in rows:
            handle.write(row.fingerprint + "\n")
            written += 1
            if progress and written % 5000 == 0:
                progress(written)
    if progress:
        progress(written)
    return written


def export_to_path(
    db_path: str,
    destination: str,
    criteria: Optional[DomainFilter] = None,
    export_format: str = "csv",
    progress: Optional[Callable[[int], None]] = None,
) -> int:
    """Export matching domains from *db_path* to *destination*.

    ``destination`` may be ``"-"`` to write to standard output.
    """
    criteria = criteria or DomainFilter()
    with DomainQuery(db_path) as query:
        rows = query.iter_all(criteria)
        if destination == "-":
            return write_rows(rows, sys.stdout, export_format, progress)
        directory = os.path.dirname(os.path.abspath(destination))
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = f"{destination}.part"
        try:
            with open(temporary, "w", encoding="utf-8", newline="") as handle:
                written = write_rows(rows, handle, export_format, progress)
            os.replace(temporary, destination)
        except OSError as exc:
            raise ExportError(f"cannot write {destination}: {exc}") from exc
        finally:
            if os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass
    return written


def format_for_path(path: str, fallback: str = "csv") -> str:
    """Guess the export format from a file extension."""
    extension = os.path.splitext(path)[1].lower().lstrip(".")
    if extension in FORMATS:
        return extension
    if extension in ("text", "list"):
        return "txt"
    return fallback
