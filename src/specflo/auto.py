"""The `specflo auto` handoff payload - the auto-mode opt-in surface.

`specflo auto` is the explicit, per-invocation opt-in that starts or continues an
unattended run from the current phase toward project completion. Like ``hook.py``
it is pure derivation over project state: it *emits a payload* and never drives a
loop, spawns a nested agent, or clears context - the seamless clear-and-reseed
trigger is the outer harness's job (REQ-05).

Strictly additive (REQ-02): the ask-first reseed (``hook.reseed_text`` /
``CONFIRMATION_DIRECTIVE``) and the manual pipeline gates are untouched; this is a
separate surface. The opt-in is the per-invocation command - no persisted
per-project auto-on default is introduced (REQ-01).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import (
    brainstorm as brainstorm_module,
    brief as brief_module,
    checkpoint as checkpoint_module,
    config,
    continuation,
    ladder as ladder_module,
    plan as plan_module,
    projects,
    review as review_module,
    spec as spec_module,
    validators,
)
from .errors import SpecfloError
from .locking import lock_path_for, locked
from .projects import COMPLETE_STATUS

# Fixed marker opening the auto-mode bootstrap section of the payload. Tests key
# on this structurally (never on verbatim wording), and later tasks grow the
# autonomy/guardrail directives beneath it without moving the marker.
BOOTSTRAP_MARKER = "== specflo auto-mode bootstrap =="

# Fixed clause markers within the bootstrap. Tests key on these structurally so
# later tasks can grow each clause's wording in place. BOUNDARY_OVERRIDE_MARKER
# labels the clause that supersedes the phase skills' pause gate (REQ-06);
# FORK_POLICY_MARKER labels the default decision-fork policy (REQ-11).
BOUNDARY_OVERRIDE_MARKER = "Boundary override:"
FORK_POLICY_MARKER = "Decision forks:"
SIDE_EFFECT_MARKER = "Irreversible / outbound actions:"
PLAN_TIME_MARKER = "Plan-time avoidance:"
COMPLETION_MARKER = "Terminal stop:"
# Labels the clause telling the continuing pass to re-emit this bootstrap so the
# loop never reverts to the manual ask-first behavior after a boundary (REQ-04).
SELF_PROPAGATE_MARKER = "Self-propagation:"

# The terminal signal `specflo advance` prints when the final phase completes the
# project ("Completed project '<slug>'."). The loop stops when it sees this - the
# CLI declares done; the loop never guesses completion (REQ-13). Kept as the
# recognised substring, matched by tests against the CLI's own output.
COMPLETION_SIGNAL = "Completed project"

# Emitted by `specflo auto` when the active project is already complete: there is
# nothing to continue, so the run stops and hands off (never re-runs a finished
# project). The session-start hook is silent for a complete project; this is the
# auto run's own stop. Deliberately free of the word "continue" so it can never
# read as a continue directive.
AUTO_COMPLETE_DIRECTIVE = (
    "The active specflo project is complete - the auto run is finished. Do NOT "
    "resume it or pick the project back up; stop and hand off to the human."
)


class AttendedOnly(SpecfloError):
    """An auto run refused because the project's level is attended work.

    The one error the auto entries let out of their never-errors guard, so the
    command exits non-zero with the message instead of emitting a payload.
    """


def refuse_attended(project) -> None:
    """Refuse an auto run on a harden project, naming its level.

    Hardening stops on the user's say, so no auto run drives it. Each entry
    calls this before it reads or writes the run state: the refusal changes
    no file.
    """
    if project.level == projects.HARDEN_LEVEL:
        raise AttendedOnly(
            f"Project {project.slug!r} is at harden level, which is attended work:"
            " hardening stops on the user's say, so `specflo auto` does not run it."
            " Work it with the specflo-execute skill instead."
        )

# Leads any guardrail stop that hands the run back to a human (the cap here in
# T-09; stall / kill-switch reuse it in T-10/T-11). Distinct from the bootstrap
# so a stop can never be mistaken for a continue directive.
ESCALATION_MARKER = "AUTO-RUN ESCALATION:"


def escalation_message(reason: str) -> str:
    """A human-escalation stop directive (no continue): ``reason`` + a hand-off."""
    return (
        f"{ESCALATION_MARKER} {reason} Stop the auto run and hand off to the "
        "human - do not start another pass."
    )


# The durable "auto off" kill switch (REQ-16): a per-project flag in the run-state
# file that specflo checks each pass. While set, a pass halts instead of
# continuing - a durable brake complementing the human interrupting the outer
# harness. KILL_MARKER leads the stop directive a killed pass emits (distinct from
# ESCALATION_MARKER so a deliberate halt reads apart from a guardrail escalation).
KILL_MARKER = "AUTO-RUN HALTED:"
KILL_DIRECTIVE = (
    f"{KILL_MARKER} the durable auto-off kill switch is set for this project. Do "
    "NOT start another auto pass; clear it with `specflo auto --on` to resume."
)
KILL_SET_MESSAGE = (
    "Auto-off kill switch SET for the active project: the next `specflo auto` pass "
    "stops instead of continuing. Clear it with `specflo auto --on`."
)
KILL_CLEARED_MESSAGE = (
    "Auto-off kill switch CLEARED for the active project: `specflo auto` resumes "
    "normal auto continuation."
)


# The stop conditions a pass can report to a machine consumer (pi-extension
# REQ-13). Loop control is read from the CLI, so the consumer never re-derives
# any of these - it stops when told and names the reason it was given. Stable
# identifiers, not prose: the human-facing wording lives in the directives above.
STOP_KILL_SWITCH = "kill-switch"
STOP_PASS_CAP = "pass-cap"
STOP_STALL = "stall"
STOP_PROJECT_COMPLETE = "project-complete"
# No longer sent: a cap never stops a run (a quick level cuts down, a fast
# level only warns). Kept so a consumer that lists the reasons still parses.
STOP_OUTGREW_LEVEL = "outgrew-level"
# A ladder level completed but the ladder cannot climb (its next branch
# exists, or the level was changed by hand); the payload says why.
STOP_LADDER_BLOCKED = "ladder-blocked"
# The latest review round asks for changes and the level has used its round
# budget: one more round or a waive is the user's call, so the run hands off.
STOP_REVIEW_BUDGET = "review-budget"
# Not a run condition but a caller-side one: no specflo root, no active project,
# or an unreadable project. The payload is empty, so there is nothing to continue.
STOP_UNAVAILABLE = "unavailable"
STOP_REASONS = (
    STOP_KILL_SWITCH,
    STOP_PASS_CAP,
    STOP_STALL,
    STOP_PROJECT_COMPLETE,
    STOP_OUTGREW_LEVEL,
    STOP_LADDER_BLOCKED,
    STOP_REVIEW_BUDGET,
    STOP_UNAVAILABLE,
)


def _pass_result(payload: str, reason: str | None) -> dict:
    """One pass's result: its ``payload``, whether to stop, and why.

    ``stop`` is derived from ``reason`` rather than passed alongside it, so the
    two can never disagree - a reason means stop, no reason means continue.
    """
    return {"payload": payload, "stop": reason is not None, "reason": reason}


def set_kill_switch(cwd: Path | None = None, killed: bool = True) -> str:
    """Set (``killed=True``) or clear (``killed=False``) the durable auto-off flag.

    The flag lives in the dedicated per-project run-state file - never a config
    key, so it is not a persisted auto-*on* default (REQ-01). Returns a
    human-facing confirmation, or ``""`` when there is no active project.
    Raises only :class:`AttendedOnly`, for a harden project, which has no
    auto run to switch.
    """
    try:
        if cwd is None:
            cwd = Path.cwd()
        found = _active_project(cwd)
        if found is None:
            return ""
        root, cfg, project = found
        refuse_attended(project)
        state = load_run_state(root, cfg, project.slug)
        if killed:
            state["killed"] = True
        else:
            state.pop("killed", None)
        save_run_state(root, cfg, project.slug, state)
        return KILL_SET_MESSAGE if killed else KILL_CLEARED_MESSAGE
    except AttendedOnly:
        raise
    except Exception:
        return ""


# The durable per-project auto-run state (REQ-14): a single dedicated JSON file
# beside the project's artifacts, holding the ephemeral pass counter (and, later,
# the stall progress signal and kill flag). It is NOT a persisted auto-*on*
# default (REQ-01) - it only exists once an auto run is under way.
AUTO_RUN_STATE_FILENAME = "auto-run.json"


def run_state_path(root: Path, cfg: config.SpecfloConfig, slug: str) -> Path:
    return projects.project_dir(root, cfg, slug) / AUTO_RUN_STATE_FILENAME


def load_run_state(root: Path, cfg: config.SpecfloConfig, slug: str) -> dict:
    """The project's auto-run state, or ``{}`` when absent/unreadable."""
    path = run_state_path(root, cfg, slug)
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def in_ladder(root: Path, cfg: config.SpecfloConfig, slug: str) -> bool:
    """Whether a ladder run is live on the project.

    False once a guardrail stopped it or the kill switch is set: a user who
    takes the project over by hand works under the attended rules.
    """
    state = load_run_state(root, cfg, slug)
    return bool(state.get("ladder")) and not (state.get("killed") or state.get("ended"))


def save_run_state(root: Path, cfg: config.SpecfloConfig, slug: str, state: dict) -> None:
    path = run_state_path(root, cfg, slug)
    with locked(lock_path_for(root, slug, path)):
        path.write_text(json.dumps(state, indent=2) + "\n")


def _mark_run_ended(root: Path, cfg: config.SpecfloConfig, slug: str) -> None:
    """Record that the auto run ended, preserving everything else in the state.

    The run-state file outlives the run that wrote it, so without this marker a
    finished run still reads as live and an attended session would be treated as
    unattended (pi-extension REQ-12). Re-reads the file rather than taking the
    caller's dict, so the pass counter written moments earlier survives - the cap
    still holds across a resumed run.

    Writes nothing when no run-state file exists: a project that never ran auto
    must not acquire one just because a pass looked at it.
    """
    if not run_state_path(root, cfg, slug).is_file():
        return
    state = load_run_state(root, cfg, slug)
    state["ended"] = True
    save_run_state(root, cfg, slug, state)


# The next step once a ladder level has completed: the run is not over, the
# next auto pass climbs, or at full level closes the ladder.
LADDER_CLIMB_STEP = (
    "This ladder level is complete. Run `specflo auto` to cut the next level's"
    " branch and move the project up."
)
LADDER_FINISH_STEP = (
    "The ladder's last level is complete. Run `specflo auto` to close the ladder:"
    " that pass writes the last row of ladder.md and hands the branches over."
)


def ladder_step(root: Path, cfg: config.SpecfloConfig, project) -> str | None:
    """The step a completed ladder level waits on, or None when there is none.

    A completed quick or fast level waits to climb; a completed full level
    waits for the pass that closes ladder.md. Never raises: an unreadable
    state reads as no ladder.
    """
    try:
        if project.status != COMPLETE_STATUS:
            return None
        ladder = load_run_state(root, cfg, project.slug).get("ladder")
        if not ladder:
            return None
        if ladder_module.next_level(project.level) is not None:
            return LADDER_CLIMB_STEP
        entry = ladder.get("levels", {}).get(project.level, {})
        return None if entry.get("row_written") else LADDER_FINISH_STEP
    except Exception:
        return None


def ladder_full_step(root: Path, cfg: config.SpecfloConfig, project) -> str | None:
    """The full-level work a ladder asks for, at brainstorm, spec and plan.

    Right after the climb every document already validates, so the plain
    "validates - advance" step would skip the work full level exists for.
    None off a ladder, below full, at execute or once complete.
    """
    try:
        if project.level != projects.FULL_LEVEL or project.status == COMPLETE_STATUS:
            return None
        ladder = load_run_state(root, cfg, project.slug).get("ladder")
        if not ladder:
            return None
        if project.phase == "brainstorm":
            review = ladder.get("levels", {}).get(project.level, {}).get("review") or []
            decisions = f" ({', '.join(review)})" if review else ""
            return (
                "Ladder at full level, with no user to interview: review each decision"
                f" made at fast level{decisions} and confirm it or supersede it with"
                " `specflo decision add --supersedes`, and take up every item in Out of"
                " scope / Deferred as new decisions; then run `specflo advance`."
            )
        if project.phase == "spec":
            return (
                "Ladder at full level: add requirements for the new decisions and the"
                " deferred work you took up, then run `specflo advance`."
            )
        if project.phase == "plan":
            return (
                "Ladder at full level: add tasks for the new requirements, then run"
                " `specflo advance`."
            )
        return None
    except Exception:
        return None


def run_under_way(root: Path, cfg: config.SpecfloConfig, project) -> bool:
    """Whether an auto run is currently live for ``project`` (pi-extension REQ-12).

    True only while all four hold: a run-state file exists (auto ran at least
    once), the project is incomplete, the kill switch is clear, and no terminal
    stop marked the run ended. Consumers ask the CLI this instead of reading the
    run-state file themselves. Never raises - an unreadable state reads as no run.
    """
    try:
        if project.status == COMPLETE_STATUS:
            # A ladder level that completed is a pause in the run, not its end,
            # while no guardrail has stopped it.
            if ladder_step(root, cfg, project) is None:
                return False
            state = load_run_state(root, cfg, project.slug)
            return not (state.get("killed") or state.get("ended"))
        if not run_state_path(root, cfg, project.slug).is_file():
            return False
        state = load_run_state(root, cfg, project.slug)
        return not (state.get("killed") or state.get("ended"))
    except Exception:
        return False


# Stall threshold (REQ-15): the number of consecutive no-forward-progress passes
# that trips a stop/escalate instead of another continue directive. A fixed
# source constant, not a user knob - a genuinely stuck loop escalates fast, well
# before the (default 50) pass cap. Verified structurally by tests, not pinned to
# a magic number here.
STALL_THRESHOLD = 3


def _count_artifact_headers(doc: str, prefix: str) -> int:
    """Count ``### <prefix>NN —`` artifact headers in ``doc``.

    A monotonic within-phase progress proxy: every `specflo decision add` /
    `requirement add` appends one such header, so the count only ever grows as the
    phase does real work (a supersede adds the new entry and keeps the old, so it
    still moves the count).
    """
    marker = f"### {prefix}"
    return sum(1 for line in doc.splitlines() if line.startswith(marker))


def progress_signal(root: Path, cfg: config.SpecfloConfig, project) -> str:
    """A derived forward-progress signal for ``project``'s current pass (REQ-15).

    Phase-aware so that *within-phase* work moves the signal, not just a phase
    advance: brainstorm counts recorded decisions, spec counts requirements, and
    plan/execute use the plan's done/total task counts; a quick project, worked
    from its brief, uses the brief's content. Advancing the phase always
    changes it too. Two consecutive passes yielding the *same* signal made no
    forward progress; :data:`STALL_THRESHOLD` such passes in a row escalate.
    Best-effort: any read failure degrades to a phase-only signal rather than
    raising, so stall detection never breaks the pass itself.
    """
    phase = project.phase
    detail = "0"
    try:
        if project.level == projects.QUICK_LEVEL:
            # A quick project is worked from its brief: any write to it is
            # forward progress, so the signal follows the brief's content.
            brief_file = brief_module.brief_path(root, cfg, project.slug)
            if brief_file.is_file():
                digest = hashlib.sha256(brief_file.read_bytes()).hexdigest()[:12]
                detail = f"brief:{digest}"
        elif phase in ("plan", "execute"):
            plan_file = project.path / plan_module.PLAN_FILENAME
            if plan_file.is_file():
                prog = plan_module.progress_from_doc(plan_file.read_text())
                detail = f"{prog['done']}/{prog['total']}"
        elif phase == "brainstorm":
            bfile = project.path / brainstorm_module.BRAINSTORM_FILENAME
            if bfile.is_file():
                detail = str(_count_artifact_headers(bfile.read_text(), "D-"))
        elif phase == "spec":
            sfile = project.path / spec_module.SPEC_FILENAME
            if sfile.is_file():
                detail = str(_count_artifact_headers(sfile.read_text(), "REQ-"))
    except Exception:
        pass
    return f"{phase}:{detail}"


def resolve_max_passes(flag: int | None, cfg_default: int | None) -> int:
    """Resolve the effective cap: flag > config default > ``DEFAULT_MAX_PASSES``.

    Any non-positive or non-int candidate (a bad flag or a corrupt config) is
    skipped, so the loop always has a sane positive backstop.
    """
    for candidate in (flag, cfg_default, config.DEFAULT_MAX_PASSES):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 1:
            return candidate
    return config.DEFAULT_MAX_PASSES

# Autonomy levels for `specflo auto` (REQ-08). `safe` (default) and `autonomous`
# stop-and-hand-off on any irreversible/outbound step; `yolo` permits them. The
# level is a per-invocation choice (the --autonomy flag) with a matching config
# default - never a persisted auto-*on* toggle (REQ-01). Both are re-exported
# from `config`, which owns the field registry: the domain and the default are
# defined once, there.
AUTONOMY_LEVELS = config.AUTONOMY_LEVELS
DEFAULT_AUTONOMY = config.DEFAULT_AUTONOMY

FLOOR_MARKER = "Always-stop floor:"

# The always-stop floor (REQ-09): a fixed, source-defined set of conditions that
# ALWAYS stop the loop and hand off - at every --autonomy level, yolo included. It
# is a source constant; no config value or level can disable or shrink it. Aligned
# with the global "never post/publish/send" rule.
ALWAYS_STOP_FLOOR = (
    "git push or force-push",
    "deleting user or untracked files",
    "outbound sends or posts (email, PR, issue, comment)",
    "spending money",
    "secret or credential operations",
)


def _boundary_override_clause() -> str:
    """The clause superseding the phase skills' boundary-pause HARD-GATE (REQ-06).

    In auto mode the specflo-brainstorm/spec/plan/execute skills' phase-boundary
    pause and wait-for-ready gate are overridden so the run flows across the
    pipeline on its own. The override lives only in this bootstrap; the manual reseed/checkpoint
    pause beat is untouched (REQ-02).
    """
    return (
        f"- {BOUNDARY_OVERRIDE_MARKER} in auto mode the phase-boundary pause and "
        "the wait-for-ready HARD-GATE of the specflo-brainstorm/spec/plan/execute "
        "skills are SUPERSEDED. Do not stop to ask at a phase boundary; once a phase "
        "validates, advance and keep going across "
        "brainstorm -> spec -> plan -> execute on your own. It covers a fast-level "
        "project's one approval before execute too: advance when the plan validates. "
        "This override applies "
        "only under this auto bootstrap - the manual pipeline's pause is unchanged."
    )


def _self_propagation_clause() -> str:
    """Re-emit this auto bootstrap on every continuing pass (REQ-04).

    The autonomy policy must not silently revert to the manual ask-first behavior
    after the first boundary. So the continuing pass carries this bootstrap
    forward: to continue, it runs ``specflo auto`` again (which re-emits this whole
    bootstrap), never the manual ``specflo checkpoint`` / ask-first reseed. The
    policy re-propagates itself across every context clear until a terminal stop or
    a guardrail escalation ends the run.
    """
    return (
        f"- {SELF_PROPAGATE_MARKER} carry this auto bootstrap forward. On every "
        "continuing pass (including after a context clear) run `specflo auto` "
        "again to re-emit it - never the manual `specflo checkpoint` / ask-first "
        "reseed. The auto policy re-propagates itself each pass so the loop does "
        "not revert to ask-first after a boundary; it ends only at a terminal stop "
        "or a guardrail escalation."
    )


def _fork_policy_clause(autonomy: str) -> str:
    """The decision-fork policy, varying by ``autonomy`` (REQ-11 default / REQ-12).

    ``safe`` (non-delegated, REQ-11): on a fork with a defensible default, take it
    and record it via ``specflo decision add``; stop and ask the human only when
    there is genuinely no defensible default.

    ``autonomous``/``yolo`` (delegated, REQ-12): decision authority is delegated,
    so decide and record *even* on a genuinely ambiguous fork (no defensible
    default) instead of stopping - still recording each assumption via
    ``specflo decision add`` (reversible via ``specflo reopen``).
    """
    if autonomy in ("autonomous", "yolo"):
        return (
            f"- {FORK_POLICY_MARKER} decision authority is delegated at "
            "--autonomy autonomous/yolo. On a fork, take the best-judgment option "
            "and record it as an assumption via `specflo decision add` (reversible "
            "via `specflo reopen`), then keep going - decide and record even when "
            "there is no defensible default (a genuinely ambiguous fork), rather "
            "than stopping to ask."
        )
    return (
        f"- {FORK_POLICY_MARKER} on a fork with a defensible default, take it and "
        "record it as an assumption via `specflo decision add` (visible and "
        "reversible via `specflo reopen`), then keep going. Stop and ask the "
        "human only when there is genuinely no defensible default."
    )


def _side_effect_clause(autonomy: str) -> str:
    """The irreversible/outbound-action gate, varying by ``autonomy`` (REQ-08).

    ``safe``/``autonomous``: stop and hand off on any irreversible or outbound
    step. ``yolo``: permit them. (T-07 adds the always-stop floor that no level
    relaxes.)
    """
    if autonomy == "yolo":
        return (
            f"- {SIDE_EFFECT_MARKER} PERMITTED at --autonomy yolo - you may "
            "perform irreversible or outbound steps without stopping, except the "
            "always-stop floor below (which no level relaxes)."
        )
    return (
        f"- {SIDE_EFFECT_MARKER} STOP and hand off to the human on any "
        "irreversible or outbound step (the default, --autonomy safe/autonomous). "
        "Do not perform it unattended."
    )


def _plan_time_avoidance_clause() -> str:
    """Author outward-facing/irreversible work as deferred draft-and-handoff (REQ-10).

    Prevention over bail-out: if the plan writes every posting/sending/publishing/
    deploying/deleting/spending step as "produce the artifact locally, hand it to
    the human", the loop only ever does reversible, internal work and never
    reaches a forced side-effect stop.
    """
    return (
        f"- {PLAN_TIME_MARKER} when authoring the plan, write any outward-facing "
        "or irreversible step (posting, sending, publishing, deploying, deleting, "
        "spending) as a deferred draft-and-handoff task - produce the artifact "
        "locally and hand it to the human, never perform it in the loop - so the "
        "run does only reversible, internal work and never reaches a forced "
        "bail-out."
    )


def _floor_clause() -> str:
    """The hardcoded always-stop floor, level-independent (REQ-09).

    Names every :data:`ALWAYS_STOP_FLOOR` condition and states that no level or
    config value relaxes it - so it reads identically under ``yolo``.
    """
    items = "; ".join(ALWAYS_STOP_FLOOR)
    return (
        f"- {FLOOR_MARKER} regardless of --autonomy level (yolo included), ALWAYS "
        f"stop and hand off to the human on any of: {items}. No level or config "
        "value relaxes this floor."
    )


def _completion_stop_clause() -> str:
    """Terminate the loop on the CLI's completion signal (REQ-13).

    Names :data:`COMPLETION_SIGNAL` as the one terminal stop - the loop waits for
    the CLI to declare done rather than guessing completion itself.
    """
    return (
        f"- {COMPLETION_MARKER} terminate the loop when `specflo advance` emits "
        f'"{COMPLETION_SIGNAL}" (the CLI declares the project done - never guess '
        "completion yourself). Stop and hand off; do not start another pass."
    )


def auto_bootstrap(phase: str, autonomy: str = DEFAULT_AUTONOMY) -> str:
    """Return the auto-mode bootstrap directive block for ``phase`` at ``autonomy``.

    The bootstrap is the standing autonomy policy + guardrail stop-conditions the
    unattended run carries: an opt-in framing header followed by the directive
    clauses. The side-effect clause varies by ``autonomy`` level (REQ-08); later
    tasks grow the remaining guardrail clauses.
    """
    header = (
        f"{BOOTSTRAP_MARKER}\n"
        f"You are running in specflo auto mode at the '{phase}' phase: an "
        "explicit, per-invocation unattended run that continues the specflo "
        "pipeline from here toward project completion. specflo only emits this "
        "directive - it does not drive the loop or clear context for you."
    )
    clauses = [
        _boundary_override_clause(),
        _self_propagation_clause(),
        _fork_policy_clause(autonomy),
        _side_effect_clause(autonomy),
        _floor_clause(),
        _plan_time_avoidance_clause(),
        _completion_stop_clause(),
    ]
    return "\n".join([header, "", *clauses])


# Fixed marker opening the generated next-step block - part 3 of the reseed
# payload (REQ-03). Tests key on it structurally so its wording can grow in place.
NEXT_STEP_MARKER = "== specflo next step =="

# The phase -> phase-skill name map, re-exported from the continuation module so
# there is exactly one copy (REQ-04). Kept under this name because the payload's
# public surface has always exposed it here.
PHASE_SKILLS = continuation.PHASE_SKILLS


def next_step_block(phase: str, do_next: str) -> str:
    """The compact next-step block specflo derives from the current phase (REQ-03).

    Part 3 of the reseed payload: this marker followed by the shared continuation
    - the phase and its immediate next action (``do_next``, the same derived hint
    the checkpoint shows), the phase skill carrying it, and the clear-point. The
    skill is a pointer only - the named action stands on its own - so a
    freshly-cleared session can act on the payload alone without re-deriving state
    or needing the skill loaded.

    The wording comes wholly from :func:`continuation.build_continuation`, the one
    producer of continuation text (REQ-04): this block adds only its marker. The
    continuation names the manual resume command alongside `specflo auto` (D-02),
    which is harmless here - the bootstrap's self-propagation clause outranks it
    for an agent inside an auto run (D-05).
    """
    return f"{NEXT_STEP_MARKER}\n{continuation.build_continuation(phase, do_next)}"


# Opens the clause a ladder run adds to every pass's payload. Tests key on it.
LADDER_MARKER = "Ladder run:"


def _ladder_clause(project, record: dict) -> str:
    """What the agent needs to know on every pass of a ladder run."""
    clause = (
        f"- {LADDER_MARKER} this run climbs quick, then fast, then full; you are at"
        f" {project.level} level on branch `{ladder_module.branch_name(project.slug, project.level)}`."
        " Commit this level's work on that branch and do not switch branches:"
        + (
            " when the level completes, the next `specflo auto` pass closes the"
            " ladder and hands the branches over."
            if project.level == projects.FULL_LEVEL else
            " when the level completes, the next `specflo auto` pass cuts the next"
            " level's branch and moves the project up."
        )
        + " This supersedes the Terminal"
        f' stop clause: at every level, `specflo advance` printing "{COMPLETION_SIGNAL}"'
        " ends only that level, not the run; do not stop, run `specflo auto` again."
        " The run ends when a pass says the ladder is complete. Never push, and"
        " never delete, rename or reset a branch."
    )
    if project.level == projects.FULL_LEVEL:
        review = record.get("levels", {}).get(project.level, {}).get("review") or []
        decisions = f" ({', '.join(review)})" if review else ""
        clause += (
            " At full level there is no user to interview, so do the full-level work"
            f" yourself: review each decision made at fast level{decisions} and confirm"
            " it or supersede it with `specflo decision add --supersedes`, take up every"
            " item in the brainstorm's Out of scope / Deferred section, extend the"
            " spec and plan to match, and close the open review round with a"
            " fresh-context review before you complete the level: take it back with"
            " `specflo review start` when the work is committed, so a round nobody"
            " wrote into yet takes the commit its reviewer reads."
        )
    return clause


def start_ladder(cwd: Path | None = None) -> None:
    """Start a ladder run on the active project, or raise naming why not.

    Cuts the quick branch, writes ladder.md, and marks the run state as a
    ladder, so every later pass of `specflo auto` continues it.
    """
    root = config.find_root(cwd or Path.cwd())
    if root is None:
        raise SpecfloError("Not a specflo project. Run `specflo init` first.")
    cfg = config.load_config(root)
    if cfg.active_project is None:
        raise SpecfloError("No active project. Run `specflo new <name> --level quick`.")
    slug = cfg.active_project
    state = load_run_state(root, cfg, slug)
    if state.get("ladder"):
        raise SpecfloError(
            f"Project {slug!r} already has a ladder; continue it with `specflo auto`"
            " (after `specflo auto --on` if the kill switch is set)."
        )
    if state.get("killed"):
        raise SpecfloError(
            f"The kill switch is set for {slug!r}, so no ladder starts. Clear it with"
            " `specflo auto --on` first."
        )
    record = ladder_module.start(root, cfg, slug)
    state["ladder"] = record
    state.pop("ended", None)
    save_run_state(root, cfg, slug, state)


def _reseed_payload(
    root: Path, cfg: config.SpecfloConfig, project, autonomy: str, extra: str | None = None
) -> str:
    """Assemble the self-contained three-part reseed payload for a continuing pass.

    In order (REQ-03): (1) the auto-mode bootstrap for the current phase/autonomy;
    (2) the verbatim ``specflo checkpoint`` render - byte-for-byte, so read-first
    files, do-next and any milestone/boundary beat ride along unchanged; and (3)
    the generated :func:`next_step_block`, derived from the *same* checkpoint
    payload so its next action can't drift from the checkpoint's. A fresh session
    handed only this can act without re-deriving state.

    Read-only over ``checkpoint`` (``build_checkpoint`` / ``render_checkpoint``);
    it never touches the ask-first reseed or the advance gate (REQ-02).
    """
    bootstrap = auto_bootstrap(project.phase, autonomy=autonomy)
    state = load_run_state(root, cfg, project.slug)
    if state.get("ladder"):
        bootstrap += "\n" + _ladder_clause(project, state["ladder"])
    if extra:
        bootstrap += "\n" + extra
    payload = checkpoint_module.build_checkpoint(root, project, cfg=cfg)
    checkpoint_text = checkpoint_module.render_checkpoint(payload)
    step = next_step_block(payload["phase"], payload["do_next"])
    # checkpoint_text ends in a newline; the "\n" before ``step`` yields one blank
    # line between the verbatim checkpoint and the next-step block while keeping
    # checkpoint_text present as an exact contiguous substring.
    return f"{bootstrap}\n\n{checkpoint_text}\n{step}"


def _active_project(cwd: Path):
    """``(root, cfg, project)`` for the active project found from ``cwd``, or ``None``.

    Mirrors the resolver in ``hook.py``; kept local so ``auto`` stays an
    independent, additive surface. May raise on a corrupt project; callers run it
    inside their own never-errors guard.
    """
    root = config.find_root(cwd)
    if root is None:
        return None
    cfg = config.load_config(root)
    if cfg.active_project is None:
        return None
    return root, cfg, projects.load_project(root, cfg, cfg.active_project)


def resolve_autonomy(autonomy: str | None, cfg_default: str | None) -> str:
    """Resolve the effective autonomy level: flag > config default > ``safe``.

    Any unknown value (a stale config or a bad hand-edit) falls back to the
    conservative :data:`DEFAULT_AUTONOMY`, so an auto run never *widens* its
    side-effect gate by accident.
    """
    level = autonomy or cfg_default or DEFAULT_AUTONOMY
    return level if level in AUTONOMY_LEVELS else DEFAULT_AUTONOMY


def auto_text(cwd: Path | None = None, autonomy: str | None = None) -> str:
    """Return the auto handoff payload for the active project found from ``cwd``.

    The autonomy level is resolved by :func:`resolve_autonomy` - an explicit
    ``autonomy`` (the --autonomy flag) wins, else the project config's default,
    else ``safe`` (REQ-08). The payload is the self-contained three-part reseed
    payload (:func:`_reseed_payload`): the auto-mode bootstrap, the verbatim
    ``specflo checkpoint`` render, and the generated next-step block (REQ-03).

    Returns ``""`` and never raises when there is nothing to emit (no specflo
    root, no active project, or an unreadable project) - even resolving the
    current directory happens inside the guard, so `specflo auto` is safe to
    invoke at any phase and cannot break on a half-set-up tree. Raises only
    :class:`AttendedOnly`, for a harden project.
    """
    try:
        if cwd is None:
            cwd = Path.cwd()
        found = _active_project(cwd)
        if found is None:
            return ""
        root, cfg, project = found
        refuse_attended(project)
        # A finished project has nothing to continue: stop and hand off, never
        # re-run it (REQ-13). No bootstrap (continue directive) is emitted.
        if project.status == COMPLETE_STATUS:
            return AUTO_COMPLETE_DIRECTIVE
        level = resolve_autonomy(autonomy, getattr(cfg, "autonomy", None))
        return _reseed_payload(root, cfg, project, level)
    except AttendedOnly:
        raise
    except Exception:
        return ""


def auto_pass_result(
    cwd: Path | None = None,
    autonomy: str | None = None,
    max_passes: int | None = None,
) -> dict:
    """Advance the auto run by one pass and return its machine-readable result.

    ``{"payload": str, "stop": bool, "reason": str | None}`` - the directive text
    the pass emits, whether the loop must stop, and which stop condition applied
    (one of :data:`STOP_REASONS`, ``None`` on a continuable pass). Loop control is
    decided here and read by the caller, never re-derived by it (pi-extension
    REQ-13): the consumer needs no kill-switch check, pass counter or cap of its
    own.

    The stateful entry `specflo auto` invokes. It increments the durable per-
    project pass counter (a dedicated run-state file, never a config auto-on
    default), records the phase forward-progress signal, and reports:

    - the complete-project stop (:data:`AUTO_COMPLETE_DIRECTIVE`) if the project
      is already finished - without touching the counter;
    - a human-escalation stop (:func:`escalation_message`) when the phase makes no
      forward progress across :data:`STALL_THRESHOLD` consecutive passes (REQ-15)
      or once the pass count reaches the cap (REQ-14) - no continue directive;
    - otherwise the self-contained three-part :func:`_reseed_payload` for the
      current phase (bootstrap + verbatim checkpoint + generated next-step).

    Nothing to emit (no root, no active project, unreadable project) is an
    empty payload stopping on :data:`STOP_UNAVAILABLE`, so a machine consumer
    always gets the same three keys back. The one raise is :class:`AttendedOnly`
    for a harden project, before the pass counts or writes anything.
    """
    try:
        if cwd is None:
            cwd = Path.cwd()
        found = _active_project(cwd)
        if found is None:
            return _pass_result("", STOP_UNAVAILABLE)
        root, cfg, project = found
        refuse_attended(project)
        state = load_run_state(root, cfg, project.slug)
        ladder = state.get("ladder")
        climbing = (
            ladder is not None and project.status == COMPLETE_STATUS
            and ladder_module.next_level(project.level) is not None
        )
        if project.status == COMPLETE_STATUS and not climbing:
            directive = AUTO_COMPLETE_DIRECTIVE
            if ladder is not None:
                # The ladder's top level finished: its row closes ladder.md.
                try:
                    directive = ladder_module.finish(root, cfg, project.slug, ladder)
                except SpecfloError as exc:
                    _mark_run_ended(root, cfg, project.slug)
                    return _pass_result(escalation_message(str(exc)), STOP_LADDER_BLOCKED)
                state["ladder"] = ladder
                save_run_state(root, cfg, project.slug, state)
            _mark_run_ended(root, cfg, project.slug)
            return _pass_result(directive, STOP_PROJECT_COMPLETE)
        cap = resolve_max_passes(max_passes, getattr(cfg, "auto_max_passes", None))
        # Kill switch (REQ-16): a set auto-off flag halts before this counts as a
        # pass - a killed pass is a brake, not forward progress, so it neither
        # advances the counter nor emits a continue directive.
        if state.get("killed"):
            _mark_run_ended(root, cfg, project.slug)
            return _pass_result(KILL_DIRECTIVE, STOP_KILL_SWITCH)
        # A ladder level that completed: cut the next branch and move up, then
        # carry on with this pass at the new level.
        # A pass that reaches the cap stops without cutting a branch first.
        climbing = climbing and int(state.get("passes", 0)) + 1 < cap
        if climbing:
            try:
                ladder_module.climb(root, cfg, project.slug, ladder)
            except SpecfloError as exc:
                state["ladder"] = ladder
                save_run_state(root, cfg, project.slug, state)
                _mark_run_ended(root, cfg, project.slug)
                return _pass_result(escalation_message(str(exc)), STOP_LADDER_BLOCKED)
            state["ladder"] = ladder
            save_run_state(root, cfg, project.slug, state)
            project = projects.load_project(root, cfg, project.slug)
        # The review budget is spent: the next round, or a waive, is the
        # user's choice, so the pass hands off without counting. A ladder has
        # no user to ask: it waives the level's review and goes on.
        extras = []
        if project.phase == "execute":
            budget = review_module.budget(root, cfg, project.slug)
            if budget["spent"] and ladder is None:
                _mark_run_ended(root, cfg, project.slug)
                return _pass_result(
                    escalation_message(review_module.budget_message(budget)),
                    STOP_REVIEW_BUDGET,
                )
            if budget["spent"]:
                extras.append(ladder_module.waive_for_budget(root, cfg, project.slug))
        # Over the level's cap (a fact of the documents): the run never stops
        # or moves up for it. Quick cuts down to one check; fast only warns.
        outgrew = validators.outgrown(root, cfg, project, unattended=True)
        if outgrew is not None:
            extras.append(ladder_module.over_cap_clause(outgrew, ladder is not None))
        extra = "\n".join(extras) or None
        passes = int(state.get("passes", 0)) + 1
        state["passes"] = passes
        # This pass continues the run, so any end marker left by an earlier stop
        # is stale - a cleared kill switch or a resumed run is live again.
        state.pop("ended", None)
        # Stall detection (REQ-15): compare this pass's forward-progress signal
        # with the last recorded one. Unchanged -> extend the no-progress streak;
        # changed (or the first-ever pass, which is the baseline) -> reset it.
        signal = progress_signal(root, cfg, project)
        if state.get("progress_signal") == signal:  # None (first pass) never matches
            stalled = int(state.get("stall_count", 0)) + 1
        else:
            stalled = 0
        state["progress_signal"] = signal
        state["stall_count"] = stalled
        save_run_state(root, cfg, project.slug, state)
        if stalled >= STALL_THRESHOLD:
            _mark_run_ended(root, cfg, project.slug)
            return _pass_result(
                escalation_message(
                    f"no forward progress across {stalled} consecutive passes at the "
                    f"'{project.phase}' phase (stall threshold {STALL_THRESHOLD})."
                ),
                STOP_STALL,
            )
        if passes >= cap:
            _mark_run_ended(root, cfg, project.slug)
            return _pass_result(
                escalation_message(f"reached the auto-run pass cap of {cap} passes."),
                STOP_PASS_CAP,
            )
        level = resolve_autonomy(autonomy, getattr(cfg, "autonomy", None))
        return _pass_result(_reseed_payload(root, cfg, project, level, extra=extra), None)
    except AttendedOnly:
        raise
    except Exception:
        return _pass_result("", STOP_UNAVAILABLE)


def auto_pass(
    cwd: Path | None = None,
    autonomy: str | None = None,
    max_passes: int | None = None,
) -> str:
    """The prose half of :func:`auto_pass_result`: just the pass's directive text.

    What the default `specflo auto` prints, unchanged - one pass, one payload.
    ``""`` when there is nothing to emit.
    """
    return auto_pass_result(cwd, autonomy=autonomy, max_passes=max_passes)["payload"]


def mark_level_end(root: Path, cfg: config.SpecfloConfig, project) -> None:
    """Record where and when a ladder level completed, as it completes.

    Called by `specflo advance`, so a level's row measures the level itself
    rather than the time until the next auto pass. Does nothing off a ladder.
    """
    state = load_run_state(root, cfg, project.slug)
    ladder = state.get("ladder")
    if not ladder or project.level not in ladder.get("levels", {}):
        return
    ladder_module.mark_end(root, ladder, project.level)
    save_run_state(root, cfg, project.slug, state)
