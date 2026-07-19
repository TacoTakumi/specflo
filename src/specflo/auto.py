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

import json
from pathlib import Path

from . import (
    brainstorm as brainstorm_module,
    config,
    plan as plan_module,
    projects,
    spec as spec_module,
)
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

# The terminal signal `specflo advance` prints when the final phase completes the
# project ("Completed project '<slug>'."). The loop stops when it sees this - the
# CLI declares done; the loop never guesses completion (REQ-13). Kept as the
# recognised substring, matched by tests against the CLI's own output.
COMPLETION_SIGNAL = "Completed project"

# Emitted by `specflo auto` when the active project is already complete: there is
# nothing to continue, so the run stops and hands off (never re-runs a finished
# project). Counterpart to hook.COMPLETE_DIRECTIVE; deliberately free of the word
# "continue" so it can never read as a continue directive.
AUTO_COMPLETE_DIRECTIVE = (
    "The active specflo project is complete - the auto run is finished. Do NOT "
    "resume it or pick the project back up; stop and hand off to the human."
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


def set_kill_switch(cwd: Path | None = None, killed: bool = True) -> str:
    """Set (``killed=True``) or clear (``killed=False``) the durable auto-off flag.

    The flag lives in the dedicated per-project run-state file - never a config
    key, so it is not a persisted auto-*on* default (REQ-01). Returns a
    human-facing confirmation, or ``""`` when there is no active project. Never
    raises.
    """
    try:
        if cwd is None:
            cwd = Path.cwd()
        found = _active_project(cwd)
        if found is None:
            return ""
        root, cfg, project = found
        state = load_run_state(root, cfg, project.slug)
        if killed:
            state["killed"] = True
        else:
            state.pop("killed", None)
        save_run_state(root, cfg, project.slug, state)
        return KILL_SET_MESSAGE if killed else KILL_CLEARED_MESSAGE
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


def save_run_state(root: Path, cfg: config.SpecfloConfig, slug: str, state: dict) -> None:
    run_state_path(root, cfg, slug).write_text(json.dumps(state, indent=2) + "\n")


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
    plan/execute use the plan's done/total task counts. Advancing the phase always
    changes it too. Two consecutive passes yielding the *same* signal made no
    forward progress; :data:`STALL_THRESHOLD` such passes in a row escalate.
    Best-effort: any read failure degrades to a phase-only signal rather than
    raising, so stall detection never breaks the pass itself.
    """
    phase = project.phase
    detail = "0"
    try:
        if phase in ("plan", "execute"):
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
# default - never a persisted auto-*on* toggle (REQ-01). DEFAULT_AUTONOMY mirrors
# config.DEFAULT_AUTONOMY (kept in sync; not imported to avoid a config->auto cycle).
AUTONOMY_LEVELS = ("safe", "autonomous", "yolo")
DEFAULT_AUTONOMY = "safe"

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

    In auto mode the brainstorm/spec/plan/execute skills' phase-boundary pause and
    wait-for-ready gate are overridden so the run flows across the pipeline on its
    own. The override lives only in this bootstrap; the manual reseed/checkpoint
    pause beat is untouched (REQ-02).
    """
    return (
        f"- {BOUNDARY_OVERRIDE_MARKER} in auto mode the phase-boundary pause and "
        "the wait-for-ready HARD-GATE of the brainstorm/spec/plan/execute skills "
        "are SUPERSEDED. Do not stop to ask at a phase boundary; once a phase "
        "validates, advance and keep going across "
        "brainstorm -> spec -> plan -> execute on your own. This override applies "
        "only under this auto bootstrap - the manual pipeline's pause is unchanged."
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
        _fork_policy_clause(autonomy),
        _side_effect_clause(autonomy),
        _floor_clause(),
        _plan_time_avoidance_clause(),
        _completion_stop_clause(),
    ]
    return "\n".join([header, "", *clauses])


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
    else ``safe`` (REQ-08). For now the payload is the auto-mode bootstrap for the
    project's current phase; later tasks wrap it into the self-contained
    three-part reseed payload (bootstrap + verbatim checkpoint + generated
    next-step).

    Returns ``""`` and never raises when there is nothing to emit (no specflo
    root, no active project, or an unreadable project) - even resolving the
    current directory happens inside the guard, so `specflo auto` is safe to
    invoke at any phase and cannot break on a half-set-up tree.
    """
    try:
        if cwd is None:
            cwd = Path.cwd()
        found = _active_project(cwd)
        if found is None:
            return ""
        _root, cfg, project = found
        # A finished project has nothing to continue: stop and hand off, never
        # re-run it (REQ-13). No bootstrap (continue directive) is emitted.
        if project.status == COMPLETE_STATUS:
            return AUTO_COMPLETE_DIRECTIVE
        level = resolve_autonomy(autonomy, getattr(cfg, "autonomy", None))
        return auto_bootstrap(project.phase, autonomy=level)
    except Exception:
        return ""


def auto_pass(
    cwd: Path | None = None,
    autonomy: str | None = None,
    max_passes: int | None = None,
) -> str:
    """Advance the auto run by one pass and return the directive for it.

    The stateful entry `specflo auto` invokes. It increments the durable per-
    project pass counter (a dedicated run-state file, never a config auto-on
    default), records the phase forward-progress signal, and returns:

    - the complete-project stop (:data:`AUTO_COMPLETE_DIRECTIVE`) if the project
      is already finished - without touching the counter;
    - a human-escalation stop (:func:`escalation_message`) when the phase makes no
      forward progress across :data:`STALL_THRESHOLD` consecutive passes (REQ-15)
      or once the pass count reaches the cap (REQ-14) - no continue directive;
    - otherwise the pure :func:`auto_bootstrap` payload for the current phase.

    Returns ``""`` and never raises when there is nothing to emit (no root, no
    active project, unreadable project).
    """
    try:
        if cwd is None:
            cwd = Path.cwd()
        found = _active_project(cwd)
        if found is None:
            return ""
        root, cfg, project = found
        if project.status == COMPLETE_STATUS:
            return AUTO_COMPLETE_DIRECTIVE
        cap = resolve_max_passes(max_passes, getattr(cfg, "auto_max_passes", None))
        state = load_run_state(root, cfg, project.slug)
        # Kill switch (REQ-16): a set auto-off flag halts before this counts as a
        # pass - a killed pass is a brake, not forward progress, so it neither
        # advances the counter nor emits a continue directive.
        if state.get("killed"):
            return KILL_DIRECTIVE
        passes = int(state.get("passes", 0)) + 1
        state["passes"] = passes
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
            return escalation_message(
                f"no forward progress across {stalled} consecutive passes at the "
                f"'{project.phase}' phase (stall threshold {STALL_THRESHOLD})."
            )
        if passes >= cap:
            return escalation_message(
                f"reached the auto-run pass cap of {cap} passes."
            )
        level = resolve_autonomy(autonomy, getattr(cfg, "autonomy", None))
        return auto_bootstrap(project.phase, autonomy=level)
    except Exception:
        return ""
