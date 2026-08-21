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

# The one human-owned area (REQ-04). Everything between the markers is carried
# byte-for-byte across regenerations; everything outside them is CLI-owned.
NOTES_BEGIN = "<!-- notes:begin -->"
NOTES_END = "<!-- notes:end -->"
_EMPTY_NOTES = "\n"


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


def _extract_notes(existing: str | None) -> str:
    """The notes content carried into the next regeneration.

    Intact markers: the text between them, verbatim - this is the REQ-04
    byte-for-byte contract. A damaged pair is best-effort recovery: with the
    end marker lost the notes are the tail after the begin marker; with the
    begin marker lost they are what sits between the Notes heading and the end
    marker. No file, or no trace of the section, starts empty.
    """
    if existing is None:
        return _EMPTY_NOTES
    begin = existing.find(NOTES_BEGIN)
    end = existing.rfind(NOTES_END)
    if begin != -1:
        start = begin + len(NOTES_BEGIN)
        return existing[start:end] if end >= start else existing[start:]
    if end != -1:
        heading = existing.rfind("## Notes", 0, end)
        start = heading + len("## Notes") if heading != -1 else 0
        return existing[start:end]
    return _EMPTY_NOTES


def render_index(cfg: SpecfloConfig, items: list[Project], notes: str = _EMPTY_NOTES) -> str:
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
        "## Notes",
        NOTES_BEGIN,
    ]
    if not notes:
        notes = _EMPTY_NOTES
    elif not notes.endswith("\n"):
        # Only reachable through damage recovery; an intact section always ends
        # with the newline before its end-marker line.
        notes += "\n"
    return "\n".join(lines) + notes + NOTES_END + "\n"


def write_index(root: Path, cfg: SpecfloConfig) -> Path:
    """(Re)generate the index from the projects on disk. Returns its path.

    The read-extract-write of the preserved Notes section runs as one critical
    section inside the locking seam.
    """
    path = index_path(root, cfg)
    with locked(lock_path_for(root, _LOCK_SCOPE, path)):
        existing = path.read_text() if path.is_file() else None
        notes = _extract_notes(existing)
        path.write_text(render_index(cfg, list_projects(root, cfg), notes))
    return path
