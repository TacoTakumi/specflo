"""The specflo daemon: the process that hosts projects for CLI clients.

The daemon owns a root of its own. Under it live a ``projects`` directory,
holding the only copy of every hosted project's artifacts, and the state
store. The root is a specflo root like any checkout, so the local service
runs on it unchanged: what a client reaches over HTTP, the daemon performs
in-process on these files.

This package imports no web framework. The application lives in
:mod:`specflo.daemon.app` behind the ``serve`` extra; the root layout here
is plain files, so the command line and the tests can reason about it
without the extra installed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..config import config_path, init_config

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8741
HEALTH_PATH = "/health"

PROJECTS_DIRNAME = "projects"
STATE_STORE_FILENAME = "state.db"


def prepare_root(root: Path) -> Path:
    """Make ``root`` a daemon root, creating what a first start needs.

    Writes the specflo config that points at the daemon's own ``projects``
    directory, creates that directory, and creates the SQLite state store.
    Idempotent: an existing root, its config, its projects and its store are
    left exactly as they are.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if not config_path(root).is_file():
        init_config(root, projects_dir=PROJECTS_DIRNAME)
    (root / PROJECTS_DIRNAME).mkdir(exist_ok=True)
    store = root / STATE_STORE_FILENAME
    if not store.is_file():
        sqlite3.connect(store).close()
    return root
