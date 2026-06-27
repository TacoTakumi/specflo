"""The `specflo status` view — derived "where are we" state for the active project.

Both the ``status`` command and the SessionStart reseed hook surface the same
"where are we" snapshot. Deriving it once (:func:`build_status`) and rendering it
to the text block both print (:func:`render_status`) keeps the CLI and the hook
from drifting: what the user sees at session start *is* ``specflo status``.

Mirrors ``checkpoint.py``/``guide.py``: pure derivation + a renderer, no I/O of
its own beyond reading the artifacts the status already derives from.
"""

from __future__ import annotations

from pathlib import Path

from . import checkpoint, plan, projects, workflow
from .config import SpecfloConfig, display_path


def build_status(root: Path, cfg: SpecfloConfig, project: projects.Project) -> dict:
    """Derive the status payload for the active ``project`` (read-only).

    This is the dict emitted by ``specflo status --json``; ``dir`` is kept
    absolute for machine consumers, while :func:`render_status` relativizes it
    for humans.
    """
    progress = None
    if project.phase in ("plan", "execute") and plan.plan_path(root, cfg, project.slug).is_file():
        progress = plan.plan_progress(root, cfg, project.slug)
    complete = project.status == projects.COMPLETE_STATUS
    info = {
        "initialized": True,
        "active_project": project.slug,
        "name": project.name,
        "dir": str(project.path),
        "phase": project.phase,
        "status": project.status,
        "next_phase": workflow.next_phase(project.phase),
        "next_step": workflow.next_step(project.phase, progress=progress, complete=complete),
        "checkpoint": display_path(checkpoint.checkpoint_path(root, cfg, project.slug), root),
    }
    if progress is not None:
        info["progress"] = progress
    return info


def render_status(root: Path, info: dict) -> str:
    """Render the human-readable status block from a :func:`build_status` payload."""
    name, slug = info["name"], info["active_project"]
    label = name if name == slug else f"{name} ({slug})"
    lines = [
        f"Project: {label}",
        f"Dir:     {display_path(Path(info['dir']), root)}",
    ]
    phase_line = f"Phase:   {info['phase']}"
    if info["status"] == projects.COMPLETE_STATUS:
        phase_line += "  (complete)"
    lines.append(phase_line)
    if "progress" in info:
        p = info["progress"]
        nxt = " · next: " + ", ".join(p["next_actionable"]) if p["next_actionable"] else ""
        lines.append(f"Tasks:   {p['done']}/{p['total']} done{nxt}")
    lines.append(f"Next:    {info['next_step']}")
    lines.append("Resume:  specflo checkpoint")
    return "\n".join(lines)
