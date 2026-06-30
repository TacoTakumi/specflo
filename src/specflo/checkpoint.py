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

from . import plan as plan_module, workflow
from .brainstorm import BRAINSTORM_FILENAME
from .config import SpecfloConfig, display_path
from .projects import (
    COMPLETE_STATUS,
    PROJECT_FILENAME,
    SHELVED_STATUS,
    Project,
    project_dir,
)
from .spec import SPEC_FILENAME

CHECKPOINT_FILENAME = "checkpoint.md"

# Phase artifacts in pipeline order. ``checkpoint.md`` lists only the ones that
# actually exist, so there are no dangling references and the list grows on its
# own as later artifacts (plan.md, ...) land.
_ARTIFACT_ORDER: list[str] = [BRAINSTORM_FILENAME, SPEC_FILENAME, "plan.md"]


def checkpoint_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return project_dir(root, cfg, slug) / CHECKPOINT_FILENAME


def build_checkpoint(root: Path, project: Project, today: str | None = None) -> dict:
    """Derive the resume-prompt payload for ``project`` from current state.

    Read-only: inspects which artifacts exist on disk but mutates nothing.
    """
    directory = project.path
    read_first = [display_path(directory / PROJECT_FILENAME, root, posix=True)]
    for filename in _ARTIFACT_ORDER:
        if (directory / filename).is_file():
            read_first.append(display_path(directory / filename, root, posix=True))
    shelved = project.status == SHELVED_STATUS
    plan_file = directory / plan_module.PLAN_FILENAME
    prog = None
    plan_doc = None
    # A shelved project's do_next ignores progress, so skip the plan-file read.
    if not shelved and project.phase in ("plan", "execute") and plan_file.is_file():
        plan_doc = plan_file.read_text()
        prog = plan_module.progress_from_doc(plan_doc)
    if shelved:
        # Paused: don't direct to the phase's work step — resume (or start new),
        # while the recorded phase below is preserved so resume returns to it.
        do_next = workflow.next_step(project.phase, shelved=True)
    elif project.phase == "execute":
        do_next = workflow.next_step(
            "execute", progress=prog, complete=project.status == COMPLETE_STATUS
        )
        # Stuck on a superseded dependency: surface the same targeted rewire
        # remediation as `task show`/`status`, replacing the generic hint.
        if project.status != COMPLETE_STATUS and plan_doc is not None:
            stuck = plan_module.stuck_next_step_from_doc(plan_doc)
            if stuck:
                do_next = stuck
    else:
        do_next = workflow.next_step(project.phase)
        if project.phase == "plan" and prog is not None:
            if prog["next_actionable"]:
                do_next += "  (next task: " + ", ".join(prog["next_actionable"]) + ")"
            elif prog["all_done"]:
                do_next += "  (all tasks done)"
    return {
        "project": project.slug,
        "phase": project.phase,
        "status": project.status,
        "shelved_reason": project.shelved_reason,
        "generated": today or datetime.date.today().isoformat(),
        "read_first": read_first,
        "do_next": do_next,
        "path": display_path(directory / CHECKPOINT_FILENAME, root, posix=True),
    }


def render_checkpoint(payload: dict) -> str:
    """Render the payload to the markdown written to ``checkpoint.md``."""
    shelved = payload.get("status") == SHELVED_STATUS
    subtitle = f"_phase: {payload['phase']}"
    if shelved:
        subtitle += " (shelved)"
    subtitle += f" | generated {payload['generated']}_"
    lines = [
        f"# Checkpoint - {payload['project']}",
        subtitle,
        "",
    ]
    if shelved and payload.get("shelved_reason"):
        lines += [f"**Shelved:** {payload['shelved_reason']}", ""]
    lines += [
        "## Read first",
        *(f"- {path}" for path in payload["read_first"]),
        "",
        "## Do next",
        payload["do_next"],
        "",
        "## Resume",
        "- `specflo status`     - confirm phase/step",
        "- `specflo checkpoint` - reprint this prompt",
        "",
    ]
    return "\n".join(lines)


def write_checkpoint(root: Path, project: Project, today: str | None = None) -> Path:
    """Render the checkpoint for ``project`` and write ``checkpoint.md``."""
    payload = build_checkpoint(root, project, today=today)
    path = project.path / CHECKPOINT_FILENAME
    path.write_text(render_checkpoint(payload))
    return path
