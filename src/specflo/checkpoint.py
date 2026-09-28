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

from . import index as index_module, plan as plan_module, review, validators, workflow
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
_ARTIFACT_ORDER: list[str] = [BRAINSTORM_FILENAME, SPEC_FILENAME, "plan.md", "brief.md"]


def checkpoint_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return project_dir(root, cfg, slug) / CHECKPOINT_FILENAME


def build_checkpoint(
    root: Path,
    project: Project,
    cfg: SpecfloConfig | None = None,
    today: str | None = None,
    *,
    locators: bool = False,
    test_command: str | None = None,
) -> dict:
    """Derive the resume-prompt payload for ``project`` from current state.

    Read-only: inspects which artifacts exist on disk but mutates nothing.

    ``cfg`` is threaded from the write path so brainstorm/spec/plan can derive
    honest doneness — running the phase's real validator inline (REQ-01/03).
    Without it (a bare ``build_checkpoint`` call), the static work hint stands.

    ``locators`` names each file in Read first as ``<slug>/<artifact>``, the
    locator every command prints, instead of its path under ``root``. A daemon
    asks for that: its paths name nothing on the clients it serves, and the
    locator is what ``doc show`` resolves wherever the file lives.

    ``test_command`` is the caller's checkout command, named where Do next
    calls for the whole suite; None reads ``cfg``'s, which is only right where
    ``root`` is that checkout, never on a daemon.
    """
    directory = project.path

    def name(path: Path) -> str:
        if locators:
            return f"{project.slug}/{path.stem}"
        return display_path(path, root, posix=True)

    read_first = [name(directory / PROJECT_FILENAME)]
    for filename in _ARTIFACT_ORDER:
        if (directory / filename).is_file():
            read_first.append(name(directory / filename))
    # Where the review stands, read fresh from the round files (review-rounds
    # REQ-09). None without cfg, or while the project has no rounds.
    review_info = review.review_state(root, cfg, project.slug) if cfg is not None else None
    # The latest round joins Read first only when it asked for changes (REQ-14):
    # that is the one case where the resuming session has work to do about it.
    # Listing a passing round would pull its stale findings into the next
    # reviewer's context for no gain.
    if review_info is not None and review_info["verdict"] == "changes-requested":
        read_first.append(name(directory / review_info["file"]))
    shelved = project.status == SHELVED_STATUS
    plan_file = directory / plan_module.PLAN_FILENAME
    prog = None
    plan_doc = None
    milestone = None
    boundary = None
    # A shelved project's do_next ignores progress, so skip the plan-file read.
    if not shelved and project.phase in ("plan", "execute") and plan_file.is_file():
        plan_doc = plan_file.read_text()
        prog = plan_module.progress_from_doc(plan_doc)
        # The current milestone (None on a milestone-free or all-complete plan),
        # named in the resume block at plan/execute so a resumed agent knows which
        # slice the plan is on (REQ-15). Dormant without milestones (REQ-04).
        milestone = plan_module.current_milestone_from_doc(plan_doc)
        # The soft milestone-boundary verify beat (None off a boundary): the
        # just-completed milestone's Exit checklist to verify before proceeding
        # (REQ-14). Surfaced in the resume block; never a hard stop.
        # Suppressed on a complete project: the beat invites `specflo advance`,
        # which has already happened, so it would contradict Do next.
        if project.status != COMPLETE_STATUS:
            boundary = plan_module.milestone_boundary_from_doc(plan_doc)
    if shelved:
        # Paused: don't direct to the phase's work step — resume (or start new),
        # while the recorded phase below is preserved so resume returns to it.
        do_next = workflow.next_step(project.phase, shelved=True)
    elif project.phase == "execute":
        if test_command is None and cfg is not None:
            test_command = cfg.test_command
        do_next = workflow.next_step(
            "execute", progress=prog, complete=project.status == COMPLETE_STATUS,
            review=review_info, level=project.level,
            test_command=test_command,
        )
        if cfg is not None:
            # auto imports this module, so it is read here, at call time.
            from . import auto

            ladder_next = auto.ladder_step(root, cfg, project)
            if ladder_next is not None:
                do_next = ladder_next
        # Stuck on a superseded dependency: surface the same targeted rewire
        # remediation as `task show`/`status`, replacing the generic hint.
        if project.status != COMPLETE_STATUS and plan_doc is not None:
            stuck = plan_module.stuck_next_step_from_doc(plan_doc)
            if stuck:
                do_next = stuck
    else:
        # Derived doneness (REQ-01/03): run the phase's real validator inline
        # (no memoization) — a passing validator flips the hint to offer-advance,
        # a failing or missing artifact reads as work-in-progress. cfg is threaded
        # from the write path; without it the static work hint stands.
        validates = False
        if cfg is not None:
            validator = validators.VALIDATORS.get(project.phase)
            if validator is not None:
                validates = not validator(root, cfg, project.slug)
        unattended = False
        if cfg is not None:
            from . import auto

            unattended = auto.run_under_way(root, cfg, project)
        do_next = workflow.next_step(
            project.phase, validates=validates, level=project.level, unattended=unattended
        )
        if cfg is not None:
            do_next = auto.ladder_full_step(root, cfg, project) or do_next
        # A plan that doesn't yet validate still names its next task; once it
        # validates the offer-advance hint stands alone.
        if project.phase == "plan" and not validates and prog is not None:
            if prog["next_actionable"]:
                do_next += "  (next task: " + ", ".join(prog["next_actionable"]) + ")"
            elif prog["all_done"]:
                do_next += "  (all tasks done)"
    return {
        "project": project.slug,
        "phase": project.phase,
        "status": project.status,
        "execution": project.execution,
        "shelved_reason": project.shelved_reason,
        # The prior-projects rule (project-index REQ-06): the checkpoint is what
        # carries it into the session-start hook payload. None without cfg, or
        # while no completed project exists.
        "rule": index_module.rule_line(root, cfg) if cfg is not None else None,
        "generated": today or datetime.date.today().isoformat(),
        "read_first": read_first,
        "do_next": do_next,
        "milestone": milestone,
        "boundary": boundary,
        # Why the project has outgrown its level, while it has; derived from
        # the documents, so it needs cfg like the validators do.
        "outgrew": (
            validators.outgrown(root, cfg, project)
            if cfg is not None and project.status != COMPLETE_STATUS else None
        ),
        "path": display_path(directory / CHECKPOINT_FILENAME, root, posix=True),
        # The checkpoint named the way every artifact is named on a command's
        # human line: the same wherever its bytes live.
        "locator": f"{project.slug}/checkpoint",
    }


def hosted_view(payload: dict) -> dict:
    """``payload`` as a client of a daemon reports it.

    A daemon builds the payload with Read first already by locator, so the
    checkpoint it stores and the one a client prints are the same text. The
    checkpoint's own ``path`` is the daemon's file, which names nothing on
    the client, so it is None here.
    """
    return {**payload, "path": None}


def render_checkpoint(payload: dict) -> str:
    """Render the payload to the markdown written to ``checkpoint.md``."""
    shelved = payload.get("status") == SHELVED_STATUS
    subtitle = f"_phase: {payload['phase']}"
    if shelved:
        subtitle += " (shelved)"
    subtitle += f" | execution: {payload['execution']}"
    subtitle += f" | generated {payload['generated']}_"
    lines = [
        f"# Checkpoint - {payload['project']}",
        subtitle,
        "",
    ]
    if shelved and payload.get("shelved_reason"):
        lines += [f"**Shelved:** {payload['shelved_reason']}", ""]
    if payload.get("rule"):
        lines += [payload["rule"], ""]
    lines += [
        "## Read first",
        *(f"- {path}" for path in payload["read_first"]),
        "",
        "## Do next",
        payload["do_next"],
    ]
    if payload.get("outgrew"):
        lines.append(f"Level: {payload['outgrew']}")
    # Name the current milestone in the resume block so a resumed agent knows
    # which slice the plan is on (REQ-15); absent on a milestone-free plan.
    milestone = payload.get("milestone")
    if milestone:
        lines.append(
            f"Current milestone: {milestone['id']} {milestone['title']} "
            f"— {milestone['done']}/{milestone['total']} done."
        )
    # The soft milestone-boundary verify beat: the just-completed milestone's Exit
    # checklist for a user-gated proceed (REQ-14). Absent off a boundary.
    boundary = payload.get("boundary")
    if boundary:
        lines += ["", "## Milestone boundary", *plan_module.boundary_beat_lines(boundary)]
    lines += [
        "",
        "## Resume",
        "- `specflo status`     - confirm phase/step",
        "- `specflo checkpoint` - reprint this prompt",
        "",
    ]
    return "\n".join(lines)


def write_checkpoint(
    root: Path,
    project: Project,
    cfg: SpecfloConfig | None = None,
    today: str | None = None,
    *,
    locators: bool = False,
) -> Path:
    """Render the checkpoint for ``project`` and write ``checkpoint.md``.

    ``cfg`` and ``locators`` are forwarded to :func:`build_checkpoint`, so the
    written checkpoint reflects derived doneness for brainstorm/spec/plan
    (REQ-01) and names files the way its readers can resolve them.
    """
    payload = build_checkpoint(root, project, cfg=cfg, today=today, locators=locators)
    path = project.path / CHECKPOINT_FILENAME
    path.write_text(render_checkpoint(payload))
    return path
