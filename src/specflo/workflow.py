"""The specflo phase model (hardcoded for v0.1).

The workflow is a fixed linear sequence of phases. This is deliberately
hardcoded for now; if we ever need fork-able, per-project workflows we can
lift this into a YAML schema (see docs/MASTER Phase 2 open questions).
"""

PHASES: list[str] = ["brainstorm", "spec", "plan", "execute"]

_NEXT_STEP: dict[str, str] = {
    "brainstorm": "Brainstorm and research; capture decisions, then write the spec.",
    "spec": "Write the spec: testable requirements and scenarios.",
    "plan": "Write the multi-phase implementation plan with dependency-ordered tasks.",
    "execute": "Execute the plan one step at a time, verifying each.",
}


def _require_known(phase: str) -> None:
    if phase not in PHASES:
        raise ValueError(
            f"Unknown phase {phase!r}. Known phases: {', '.join(PHASES)}."
        )


def next_phase(phase: str) -> str | None:
    """Return the phase after ``phase``, or None if it is the final phase."""
    _require_known(phase)
    index = PHASES.index(phase)
    if index + 1 < len(PHASES):
        return PHASES[index + 1]
    return None


def next_step(
    phase: str,
    progress: dict | None = None,
    complete: bool = False,
    shelved: bool = False,
) -> str:
    """Return a human-readable hint for what to do while in ``phase``.

    ``shelved=True`` takes precedence over everything else (a paused project is
    not advanced from any phase): the hint directs to resume or start anew. For
    the ``execute`` phase the hint is otherwise progress-aware: pass the
    ``plan_progress`` dict and/or ``complete=True`` (project finished). All other
    phases ignore ``progress``/``complete`` and return their static hint, so the
    single-argument form is unchanged.
    """
    _require_known(phase)
    if shelved:
        return (
            "Project shelved. Resume it with `specflo resume`, or start a new "
            "one with `specflo new`."
        )
    if phase == "execute":
        if complete:
            return "Project complete. Start the next piece of work with `specflo new`."
        if progress is not None and progress.get("total", 0) > 0:
            if progress.get("all_done"):
                return (
                    "All tasks done - run the final whole-branch review (fresh "
                    "context), then `specflo advance` to complete the project."
                )
            actionable = progress.get("next_actionable") or []
            if actionable:
                return f"Work the next task: {', '.join(actionable)} (`specflo task show`)."
            return (
                "Tasks remain but none are actionable - unblock or reopen one "
                "(`specflo task list`)."
            )
    return _NEXT_STEP[phase]
