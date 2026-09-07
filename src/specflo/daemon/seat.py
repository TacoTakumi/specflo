"""The agent's seat: a client checkout under the daemon root, one per hosted project.

The daemon runs an agent for a hosted project as a CLI client of itself, so
the agent needs a directory to run in that knows the daemon and the project:
the same shape a developer's checkout has after ``remote add`` and ``new
--remote``. The seat is that shape and nothing more. It holds a config
whose active project is the hosted slug, the hosted registry naming this
daemon's remote for it, and one remote entry carrying the daemon's URL and
an agent token. No artifact lives here; every read and write goes over the
wire to the daemon, which keeps the project under its own projects
directory.

Scaffolding is idempotent: a second call with the same URL and token
rewrites the same bytes, and one with a new token or URL rebinds the remote.
"""

from __future__ import annotations

from pathlib import Path

from .. import config
from ..projects import validate_slug

SEATS_DIRNAME = "seats"
# The one remote a seat knows: the daemon that scaffolded it.
REMOTE_NAME = "daemon"


def seat_dir(root: Path, slug: str) -> Path:
    """Where the daemon root keeps the seat for ``slug``."""
    return Path(root) / SEATS_DIRNAME / slug


def _remove_empty_tree(leaf: Path, *, stop: Path) -> None:
    """Remove ``leaf`` and each empty parent below ``stop``."""
    for directory in (leaf, *leaf.parents):
        if directory == stop or not directory.is_relative_to(stop):
            return
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
        else:
            return


def scaffold(root: Path, slug: str, url: str, token: str) -> Path:
    """Make, or refresh, the seat for ``slug`` and return its directory.

    ``url`` is where the daemon answers and ``token`` an agent token it
    minted. The slug is checked before it becomes a path.
    """
    slug = validate_slug(slug)
    workspace = seat_dir(root, slug)
    workspace.mkdir(parents=True, exist_ok=True)
    if not config.config_path(workspace).is_file():
        # init_config makes the projects directory; the seat keeps none, so
        # it goes straight back: a client seat holds no project of its own.
        cfg = config.init_config(workspace)
        _remove_empty_tree(workspace / cfg.projects_dir, stop=workspace)
    else:
        cfg = config.load_config(workspace)
    config.add_remote(workspace, REMOTE_NAME, url, token)
    config.record_hosted_project(workspace, slug, REMOTE_NAME)
    if cfg.active_project != slug:
        cfg.active_project = slug
        config.save_config(workspace, cfg)
    return workspace
