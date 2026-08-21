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


# Completion banners (REQ-09): stamped below the frontmatter of a completed
# project's brainstorm.md and spec.md - never plan.md, which stays the frozen
# record of how the work was actually run.
BANNER_PREFIX = "> Complete ("
_BANNER_ARTIFACTS = ("brainstorm.md", "spec.md")
_BANNER_TAILS = {
    "historical": "Decisions and requirements here do not bind new work unless restated.",
    "binding": (
        "Decisions and requirements here remain binding on new work"
        " unless explicitly superseded."
    ),
}


def index_path(root: Path, cfg: SpecfloConfig) -> Path:
    return root / cfg.projects_dir / INDEX_FILENAME


def banner_text(cfg: SpecfloConfig, project: Project) -> str:
    """The pinned one-line blockquote banner for ``project``."""
    index_ref = (Path(cfg.projects_dir) / INDEX_FILENAME).as_posix()
    return (
        f"{BANNER_PREFIX}{project.completed or 'unknown'})."
        f" Historical record - see {index_ref}."
        f" {_BANNER_TAILS[cfg.prior_projects]}"
    )


def _with_banner(text: str, banner: str) -> str:
    """``text`` with ``banner`` as the first body line below the frontmatter.

    An existing banner (any line starting with :data:`BANNER_PREFIX` in that
    position) is replaced, which is what makes stamping idempotent and lets a
    mode flip re-word it. A file with no frontmatter is stamped at the top.
    """
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        head, body = "", text
    else:
        head, body = f"---{parts[1]}---\n\n", parts[2]
    lines = body.lstrip("\n").split("\n")
    if lines and lines[0].startswith(BANNER_PREFIX):
        lines[0] = banner
    else:
        lines = [banner, ""] + lines
    return head + "\n".join(lines)


def stamp_banners(root: Path, cfg: SpecfloConfig, project: Project) -> list[Path]:
    """Stamp the completion banner into the project's brainstorm.md and spec.md.

    Idempotent; a missing artifact is skipped. Returns the stamped paths.
    """
    banner = banner_text(cfg, project)
    stamped = []
    for name in _BANNER_ARTIFACTS:
        path = project.path / name
        if not path.is_file():
            continue
        with locked(lock_path_for(root, project.slug, path)):
            text = path.read_text()
            updated = _with_banner(text, banner)
            if updated != text:
                path.write_text(updated)
        stamped.append(path)
    return stamped


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
