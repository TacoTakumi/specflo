"""The `checkpoint` resume prompt — derived, agent-facing "resume here" state.

After every state-mutating command specflo rewrites a per-project
``checkpoint.md``: a short, derived prompt telling a freshly-cleared agent which
phase we're in, which files to read first, and the concrete next action. It is
fully derived from project state (phase + existing artifacts + the workflow's
next step), so it can be written automatically and is always current.

Mirrors ``guide.py``: pure derivation (``build_checkpoint``) + a renderer
(``render_checkpoint``) + a thin writer (``write_checkpoint``).
"""

from __future__ import annotations

import datetime
from pathlib import Path

from . import workflow
from .brainstorm import BRAINSTORM_FILENAME
from .config import SpecfloConfig
from .projects import PROJECT_FILENAME, Project, project_dir
from .spec import SPEC_FILENAME

CHECKPOINT_FILENAME = "checkpoint.md"

# Phase artifacts in pipeline order. ``checkpoint.md`` lists only the ones that
# actually exist, so there are no dangling references and the list grows on its
# own as later artifacts (plan.md, ...) land.
_ARTIFACT_ORDER: list[str] = [BRAINSTORM_FILENAME, SPEC_FILENAME, "plan.md"]


def checkpoint_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return project_dir(root, cfg, slug) / CHECKPOINT_FILENAME


def _relpath(path: Path, root: Path) -> str:
    """``path`` relative to the repo root as POSIX, or absolute if outside it."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def build_checkpoint(root: Path, project: Project, today: str | None = None) -> dict:
    """Derive the resume-prompt payload for ``project`` from current state.

    Read-only: inspects which artifacts exist on disk but mutates nothing.
    """
    directory = project.path
    read_first = [_relpath(directory / PROJECT_FILENAME, root)]
    for filename in _ARTIFACT_ORDER:
        if (directory / filename).is_file():
            read_first.append(_relpath(directory / filename, root))
    return {
        "project": project.slug,
        "phase": project.phase,
        "generated": today or datetime.date.today().isoformat(),
        "read_first": read_first,
        "do_next": workflow.next_step(project.phase),
        "path": _relpath(directory / CHECKPOINT_FILENAME, root),
    }


def render_checkpoint(payload: dict) -> str:
    """Render the payload to the markdown written to ``checkpoint.md``."""
    lines = [
        f"# Checkpoint — {payload['project']}",
        f"_phase: {payload['phase']} · generated {payload['generated']}_",
        "",
        "## Read first",
        *(f"- {path}" for path in payload["read_first"]),
        "",
        "## Do next",
        payload["do_next"],
        "",
        "## Resume",
        "- `specflo status`     — confirm phase/step",
        "- `specflo checkpoint` — reprint this prompt",
        "",
    ]
    return "\n".join(lines)


def write_checkpoint(root: Path, project: Project, today: str | None = None) -> Path:
    """Render the checkpoint for ``project`` and write ``checkpoint.md``."""
    payload = build_checkpoint(root, project, today=today)
    path = project.path / CHECKPOINT_FILENAME
    path.write_text(render_checkpoint(payload))
    return path
