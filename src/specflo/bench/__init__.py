"""The model bench: how well a local model drives specflo.

The code and the data it ships with live in this package: the fixture repo,
the level requests, the held-out tests, the frozen harness configs and the
templates of the operator's arm and matrix configs, all under ``data``.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
DATA = PACKAGE / "data"
