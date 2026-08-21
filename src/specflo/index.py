"""Rendering the project index ledger: ``specflo-index.md``.

One generated markdown file at the projects dir root (project-index REQ-02):
a header whose rule line follows the ``prior_projects`` config switch
(REQ-05), then one table row per project in created-date order with the
active project marked. The file is CLI-owned and regenerated wholesale; it
is an artifact, so every write runs inside the locking seam. Pure ASCII by
contract - every literal here must stay that way.
"""

from __future__ import annotations

from pathlib import Path

from .config import SpecfloConfig, rule_text
from .locking import lock_path_for, locked
from .projects import NEEDS_SUMMARY, Project, list_projects

INDEX_FILENAME = "specflo-index.md"
# The lock-file namespace for the index. Not a project: slugs cannot start
# with an underscore (slugify strips it), so this can never collide with one.
_LOCK_SCOPE = "_index"

_HEADER = "| Project | Created | Completed | State | Summary |"
_SEPARATOR = "| --- | --- | --- | --- | --- |"


def index_path(root: Path, cfg: SpecfloConfig) -> Path:
    return root / cfg.projects_dir / INDEX_FILENAME


def _cell(text: str) -> str:
    """``text`` made safe for a table cell: no pipes or newlines of its own."""
    return text.replace("|", "\\|").replace("\n", " ")


def _row(project: Project, active: bool) -> str:
    name = f"{project.name} (active)" if active else project.name
    state = f"{project.status}/{project.phase}"
    summary = project.summary or NEEDS_SUMMARY
    cells = (name, project.created, project.completed, state, summary)
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def render_index(cfg: SpecfloConfig, items: list[Project]) -> str:
    """The whole index document for ``items``, in created-date order."""
    ordered = sorted(items, key=lambda p: (p.created, p.slug))
    lines = [
        "# Project index",
        "",
        rule_text(cfg.prior_projects),
        "",
        _HEADER,
        _SEPARATOR,
        *(_row(p, p.slug == cfg.active_project) for p in ordered),
        "",
    ]
    return "\n".join(lines)


def write_index(root: Path, cfg: SpecfloConfig) -> Path:
    """(Re)generate the index from the projects on disk. Returns its path."""
    path = index_path(root, cfg)
    with locked(lock_path_for(root, _LOCK_SCOPE, path)):
        path.write_text(render_index(cfg, list_projects(root, cfg)))
    return path
