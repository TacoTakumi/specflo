"""The model bench: how well a local model drives specflo.

The code and the data it ships with live in this package: the fixture repo,
the level requests, the held-out tests, the frozen harness configs and the
templates of the operator's configs, all under ``data``. ``specflo bench``
names the bench home in ``SPECFLO_BENCH_HOME`` for the command it runs and
the processes that command starts; a config the operator keeps there is read
in place of the shipped template.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
DATA = PACKAGE / "data"
HOME_ENV = "SPECFLO_BENCH_HOME"


def home_file(name: str) -> Path | None:
    """The operator's ``name`` in the bench home, when a home is named and holds it."""
    home = os.environ.get(HOME_ENV)
    if not home:
        return None
    path = Path(home) / name
    return path if path.is_file() else None
