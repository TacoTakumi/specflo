"""Bench test setup: the bench package lives outside src/, under bench/."""

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[2] / "bench"
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))
