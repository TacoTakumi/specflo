"""Creating and reading project artifacts.

A project is a directory under the configured projects dir containing a
``project.md`` file. The file's YAML frontmatter is the source of truth for the
project's state (name, slug, created, phase, status); the body is for humans.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import SpecfloConfig, save_config
from .errors import SpecfloError
from .locking import lock_path_for, locked
from .workflow import next_phase, resolve_reopen_target

PROJECT_FILENAME = "project.md"
INITIAL_PHASE = "brainstorm"
INITIAL_STATUS = "active"
COMPLETE_STATUS = "complete"
SHELVED_STATUS = "shelved"
# The visible stand-in written when `new` gets no --summary (project-index
# REQ-07). The index renders it as-is, so an unset summary is impossible to
# mistake for a written one.
NEEDS_SUMMARY = "(needs summary)"
# Execution modes (fan-out-plans REQ-01). A project.md without the key reads
# as linear so files written before the key existed keep their behaviour.
LINEAR_EXECUTION = "linear"
FAN_OUT_EXECUTION = "fan-out"
EXECUTION_MODES = (LINEAR_EXECUTION, FAN_OUT_EXECUTION)


@dataclass
class Project:
    name: str
    slug: str
    created: str
    phase: str
    status: str
    path: Path
    shelved_reason: str = ""
    summary: str = ""
    completed: str = ""
    execution: str = LINEAR_EXECUTION


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise SpecfloError(f"Cannot derive a project slug from {name!r}.")
    return slug


def project_dir(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return root / cfg.projects_dir / slug


def validate_execution(mode: str) -> str:
    """Return ``mode`` if it is a known execution mode, else raise naming both."""
    if mode not in EXECUTION_MODES:
        raise SpecfloError(
            f"Unknown execution mode {mode!r}: expected one of "
            + ", ".join(repr(m) for m in EXECUTION_MODES) + "."
        )
    return mode


def create_project(
    root: Path,
    cfg: SpecfloConfig,
    name: str,
    created: str | None = None,
    summary: str | None = None,
    execution: str = LINEAR_EXECUTION,
) -> Project:
    execution = validate_execution(execution)
    slug = slugify(name)
    directory = project_dir(root, cfg, slug)
    if directory.exists():
        raise SpecfloError(f"Project {slug!r} already exists at {directory}.")

    project = Project(
        name=name,
        slug=slug,
        created=created or datetime.date.today().isoformat(),
        phase=INITIAL_PHASE,
        status=INITIAL_STATUS,
        path=directory,
        summary=summary or NEEDS_SUMMARY,
        execution=execution,
    )
    directory.mkdir(parents=True)
    (directory / PROJECT_FILENAME).write_text(_render(project))
    return project


def load_project(root: Path, cfg: SpecfloConfig, slug: str) -> Project:
    path = project_dir(root, cfg, slug) / PROJECT_FILENAME
    if not path.is_file():
        raise SpecfloError(f"No project {slug!r} found at {path}.")
    fields = _parse_frontmatter(path.read_text())
    return Project(
        name=fields["name"],
        slug=fields["slug"],
        created=str(fields["created"]),
        phase=fields["phase"],
        status=fields["status"],
        path=path.parent,
        shelved_reason=str(fields.get("shelved_reason", "") or ""),
        summary=str(fields.get("summary", "") or ""),
        completed=str(fields.get("completed", "") or ""),
        execution=str(fields.get("execution") or LINEAR_EXECUTION),
    )


def list_projects(root: Path, cfg: SpecfloConfig) -> list[Project]:
    """Return every project under the configured projects dir, sorted by slug.

    Directories without a ``project.md`` are skipped, so stray folders under the
    projects dir don't break the listing.
    """
    base = root / cfg.projects_dir
    if not base.is_dir():
        return []
    return [
        load_project(root, cfg, entry.name)
        for entry in sorted(base.iterdir())
        if (entry / PROJECT_FILENAME).is_file()
    ]


def switch_project(root: Path, cfg: SpecfloConfig, name: str) -> Project:
    """Make the named project active (persisting the change) and return it.

    ``name`` is slugified, so both the slug and the original project name work.
    Raises ``SpecfloError`` if no such project exists.
    """
    slug = slugify(name)
    if not (project_dir(root, cfg, slug) / PROJECT_FILENAME).is_file():
        raise SpecfloError(
            f"No project {slug!r}. Run `specflo list` to see available projects."
        )
    project = load_project(root, cfg, slug)
    cfg.active_project = slug
    save_config(root, cfg)
    return project


def advance_project(root: Path, cfg: SpecfloConfig, slug: str) -> Project:
    """Move the project to the next phase (persisting it) and return it.

    Raises ``SpecfloError`` if the project is already at the final phase.
    """
    project = load_project(root, cfg, slug)
    nxt = next_phase(project.phase)
    if nxt is None:
        raise SpecfloError(
            f"Project {slug!r} is already at the final phase {project.phase!r}."
        )
    project.phase = nxt
    (project_dir(root, cfg, slug) / PROJECT_FILENAME).write_text(_render(project))
    return project


def reopen_project(
    root: Path, cfg: SpecfloConfig, slug: str, target: str | None = None
) -> Project:
    """Move the project's phase pointer backward (and un-complete). Persists.

    The strict inverse of ``advance_project``: bare (``target=None``) moves to the
    immediately previous phase, a named ``target`` jumps back to that earlier
    phase. An invalid target — nothing earlier, the current phase, a later phase,
    or an unknown name — raises ``SpecfloError`` (forward movement is
    ``advance``), leaving ``project.md`` unchanged. A ``complete`` project is
    un-completed (status -> active) so the reopened phase is revisitable
    (REQ-07). Only ``project.md`` is rewritten: no downstream artifact (spec.md,
    plan.md, execute work) is touched (REQ-09).
    """
    project = load_project(root, cfg, slug)
    try:
        dest = resolve_reopen_target(project.phase, target)
    except ValueError as exc:
        raise SpecfloError(str(exc)) from exc
    project.phase = dest
    if project.status == COMPLETE_STATUS:
        project.status = INITIAL_STATUS
    (project_dir(root, cfg, slug) / PROJECT_FILENAME).write_text(_render(project))
    return project


def complete_project(root: Path, cfg: SpecfloConfig, slug: str) -> Project:
    """Mark the project complete (terminal). Persists and returns it. Idempotent.

    Stamps ``completed`` with today's date (project-index REQ-07); a date
    already present is history and stays put on re-completion.
    """
    project = load_project(root, cfg, slug)
    project.status = COMPLETE_STATUS
    if not project.completed:
        project.completed = datetime.date.today().isoformat()
    (project_dir(root, cfg, slug) / PROJECT_FILENAME).write_text(_render(project))
    return project


def shelve_project(
    root: Path, cfg: SpecfloConfig, slug: str, reason: str | None = None
) -> Project:
    """Mark the project shelved without touching its phase. Persists and returns it.

    Re-shelving an already-shelved project succeeds; ``shelved_reason`` is set to
    the latest ``reason`` (empty when none is given). The phase is left unchanged
    so resume returns to it.
    """
    project = load_project(root, cfg, slug)
    project.status = SHELVED_STATUS
    project.shelved_reason = reason or ""
    (project_dir(root, cfg, slug) / PROJECT_FILENAME).write_text(_render(project))
    return project


def set_summary(root: Path, cfg: SpecfloConfig, slug: str, text: str) -> Project:
    """Set the project's one-line summary (project-index REQ-08). Persists.

    The read-modify-write runs inside the locking seam so a concurrent
    lifecycle command cannot lose the update.
    """
    text = " ".join(text.split())
    if not text:
        raise SpecfloError("Summary text must be non-empty.")
    path = project_dir(root, cfg, slug) / PROJECT_FILENAME
    with locked(lock_path_for(root, slug, path)):
        project = load_project(root, cfg, slug)
        project.summary = text
        path.write_text(_render(project))
    return project


def set_execution(
    root: Path, cfg: SpecfloConfig, slug: str, mode: str
) -> tuple[str, bool]:
    """Set the project's execution mode, rewriting only that frontmatter key.

    Returns ``(mode, changed)``; ``changed`` is False when the file already
    reads as ``mode``. The rewrite is textual so every other frontmatter key
    and the body survive byte-for-byte (fan-out-plans REQ-02). A missing key
    is appended to the frontmatter when a non-default mode is set.
    """
    mode = validate_execution(mode)
    path = project_dir(root, cfg, slug) / PROJECT_FILENAME
    with locked(lock_path_for(root, slug, path)):
        current = load_project(root, cfg, slug).execution
        if current == mode:
            return mode, False
        text = path.read_text()
        head, sep, rest = text.partition("---")
        front, sep2, body = rest.partition("---")
        lines = front.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("execution:"):
                lines[i] = f"execution: {mode}"
                break
        else:
            # No key yet: add it as the last frontmatter line (front ends
            # with the newline that precedes the closing fence).
            lines.insert(len(lines) - 1, f"execution: {mode}")
        path.write_text(head + sep + "\n".join(lines) + sep2 + body)
    return mode, True


def resume_project(root: Path, cfg: SpecfloConfig, slug: str) -> Project:
    """Un-shelve a project: status -> active, clear the reason, phase untouched.

    Persists and returns it. The ``active_project`` pointer is the caller's to
    move (mirroring how ``new`` sets it in the CLI), so this stays a pure
    project-file mutation like ``shelve_project``/``complete_project``.
    """
    project = load_project(root, cfg, slug)
    project.status = INITIAL_STATUS
    project.shelved_reason = ""
    (project_dir(root, cfg, slug) / PROJECT_FILENAME).write_text(_render(project))
    return project


def _render(project: Project) -> str:
    fields = {
        "name": project.name,
        "slug": project.slug,
        "created": project.created,
        "phase": project.phase,
        "status": project.status,
        "execution": project.execution,
    }
    # Optional fields appear only once they hold something, so a project file
    # written before they existed does not sprout empty keys on rewrite.
    if project.summary:
        fields["summary"] = project.summary
    if project.completed:
        fields["completed"] = project.completed
    if project.shelved_reason:
        fields["shelved_reason"] = project.shelved_reason
    frontmatter = yaml.safe_dump(fields, sort_keys=False).strip()
    return f"---\n{frontmatter}\n---\n\n# {project.name}\n\n_(phase: {project.phase})_\n"


def _parse_frontmatter(text: str) -> dict:
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        raise SpecfloError("Malformed project file: missing YAML frontmatter.")
    return yaml.safe_load(parts[1]) or {}
