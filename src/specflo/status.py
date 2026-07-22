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

from . import checkpoint, plan, projects, validators, workflow
from .config import SpecfloConfig, display_path


def build_status(root: Path, cfg: SpecfloConfig, project: projects.Project) -> dict:
    """Derive the status payload for the active ``project`` (read-only).

    This is the dict emitted by ``specflo status --json``; ``dir`` is kept
    absolute for machine consumers, while :func:`render_status` relativizes it
    for humans.
    """
    progress = None
    milestone = None
    boundary = None
    if project.phase in ("plan", "execute") and plan.plan_path(root, cfg, project.slug).is_file():
        progress = plan.plan_progress(root, cfg, project.slug)
        # The current milestone (None when the plan has no milestones or all are
        # complete) — a finer-grained "where are we" that stays dormant on a
        # milestone-free plan (REQ-04, REQ-15).
        milestone = plan.current_milestone(root, cfg, project.slug)
        # The soft milestone-boundary verify beat (None off a boundary): the
        # just-completed milestone's Exit checklist to verify before proceeding
        # (REQ-14). Never blocks — surfaced only, status still exits 0.
        boundary = plan.milestone_boundary(root, cfg, project.slug)
    complete = project.status == projects.COMPLETE_STATUS
    shelved = project.status == projects.SHELVED_STATUS
    # Derived doneness (REQ-01/03): for brainstorm/spec/plan, run the current
    # phase's real validator inline (no memoization) so a validating artifact
    # reads as "offer advance" and a failing/missing one as work-in-progress.
    # Execute is untouched (REQ-05) — its progress-based hint owns next_step.
    validates = False
    if project.phase in ("brainstorm", "spec", "plan") and not complete and not shelved:
        validator = validators.VALIDATORS.get(project.phase)
        if validator is not None:
            validates = not validator(root, cfg, project.slug)
    next_step = workflow.next_step(
        project.phase, progress=progress, complete=complete, shelved=shelved,
        validates=validates,
    )
    # In the stuck execute state (nothing actionable, a pending task blocked by a
    # superseded dependency), replace the generic hint with targeted rewire
    # remediation from the shared detector.
    if project.phase == "execute" and not complete and not shelved:
        stuck = plan.stuck_next_step(root, cfg, project.slug)
        if stuck:
            next_step = stuck
    info = {
        "initialized": True,
        "active_project": project.slug,
        "name": project.name,
        "dir": str(project.path),
        "phase": project.phase,
        "status": project.status,
        "next_phase": workflow.next_phase(project.phase),
        "next_step": next_step,
        "checkpoint": display_path(checkpoint.checkpoint_path(root, cfg, project.slug), root),
        # Machine-only: the percent-of-window at which the pi extension arms its
        # clear-and-continue trigger (pi-extension REQ-28). Carried here so the
        # extension reads it from the cold-start `status --json` it already
        # fetches, never by opening `.specflo/config.yaml` itself. Always
        # present and always an int - `load_config` resolves the default.
        "context_threshold_percent": cfg.context_threshold_percent,
    }
    # Only carried when meaningful (a shelved project with a reason), so active
    # and complete payloads don't ship an empty field — mirrors `progress`.
    if shelved and project.shelved_reason:
        info["shelved_reason"] = project.shelved_reason
    if progress is not None:
        info["progress"] = progress
    # Only carried when there's a current milestone, so milestone-free plans (and
    # all-complete ones) ship no milestone field — mirrors `progress`.
    if milestone is not None:
        info["milestone"] = {
            "id": milestone["id"],
            "title": milestone["title"],
            "done": milestone["done"],
            "total": milestone["total"],
        }
    # Only carried at a milestone boundary, so off-boundary and milestone-free
    # payloads ship no boundary field — mirrors `milestone`.
    if boundary is not None:
        info["boundary"] = boundary
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
    elif info["status"] == projects.SHELVED_STATUS:
        reason = info.get("shelved_reason")
        phase_line += f"  (shelved: {reason})" if reason else "  (shelved)"
    lines.append(phase_line)
    if "progress" in info:
        p = info["progress"]
        nxt = " | next: " + ", ".join(p["next_actionable"]) if p["next_actionable"] else ""
        lines.append(f"Tasks:   {p['done']}/{p['total']} done{nxt}")
    if "milestone" in info:
        m = info["milestone"]
        lines.append(f"Milestone: {m['id']} {m['title']} — {m['done']}/{m['total']} done")
    if "boundary" in info:
        lines.extend(plan.boundary_beat_lines(info["boundary"]))
    lines.append(f"Next:    {info['next_step']}")
    lines.append("Resume:  specflo checkpoint")
    return "\n".join(lines)
