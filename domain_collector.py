#!/usr/bin/env python3
"""Domain Collector - entry point.

The implementation lives in the :mod:`domaincollector` package; this file keeps
``python domain_collector.py`` working exactly as before.

    python domain_collector.py                 # GUI (falls back to headless)
    python domain_collector.py --headless      # terminal mode, Ctrl+C to stop
    python domain_collector.py --once --limit 50
    python domain_collector.py --stats
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from domaincollector.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
