#!/usr/bin/env python3
"""Domain Atlas entry point.

    python domain_atlas.py                     # desktop application
    python domain_atlas.py --headless          # terminal, Ctrl+C to stop
    python domain_atlas.py --stats             # inventory statistics
    python domain_atlas.py --export out.csv --filter-technology WordPress
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from domainatlas.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
