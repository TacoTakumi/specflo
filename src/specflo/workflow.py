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


def next_step(phase: str) -> str:
    """Return a human-readable hint for what to do while in ``phase``."""
    _require_known(phase)
    return _NEXT_STEP[phase]
