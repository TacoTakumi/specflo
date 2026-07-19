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

from pathlib import Path

from . import config, projects

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
        level = resolve_autonomy(autonomy, getattr(cfg, "autonomy", None))
        return auto_bootstrap(project.phase, autonomy=level)
    except Exception:
        return ""
