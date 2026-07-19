"""The shared clear-point-and-continue continuation emitted at every seam.

specflo's boundary outputs have to be thick enough that an agent can clear
context at a seam and resume without re-deriving state. Two seams emit one:
completing a task (`specflo task done`) and advancing a phase (`specflo
advance`) — and the `specflo auto` handoff payload carries the same text in its
next-step block.

This module is the *single* producer of that text (REQ-04). No seam composes its
own continuation string, so the three cannot drift apart and a later change to
the wording lands in exactly one place.

Two properties are load-bearing and deliberately structural:

- **Mode-agnostic (REQ-03).** :func:`build_continuation` is a pure function of
  its arguments. It reads no auto-run state, no kill-switch flag and no autonomy
  config value, so the text is identical whether or not an auto run is under way.
  In an auto run the self-propagating auto bootstrap supersedes this line anyway
  (D-01); naming both resume paths in prose (D-02) gets an auto agent the right
  command at zero cost in state or branching.
- **Harness-neutral (REQ-09).** The text names specflo commands only — never a
  harness trigger name or invocation string. specflo owns the payload, never the
  trigger; the outer harness maps this clear-point onto its own trigger.

The next-step hint is *passed in*, not derived here, so callers keep using the
one existing derivation (``workflow.next_step`` / ``checkpoint.build_checkpoint``)
and the hint cannot drift from what `status` and `checkpoint` report (REQ-05).
"""

from __future__ import annotations

# The phase -> phase-skill name map. The continuation points a resumed agent at
# the skill that carries the current phase, so a fresh session knows which one to
# invoke (REQ-06). It is a *pointer, not a dependency*: the continuation already
# names the concrete next action, so the run proceeds even if the skill is
# unavailable. The four phase skills happen to share their phase's name.
PHASE_SKILLS: dict[str, str] = {
    "brainstorm": "brainstorm",
    "spec": "spec",
    "plan": "plan",
    "execute": "execute",
}

# Fixed marker opening the clear-point sentence. Tests — and any outer harness
# grepping prose rather than `--json` — key on this substring, so the sentence
# around it can grow in place without breaking them.
CLEAR_POINT_MARKER = "You may clear context now"


def phase_skill(phase: str) -> str:
    """The specflo phase-skill name carrying ``phase`` (the phase name itself)."""
    return PHASE_SKILLS.get(phase, phase)


def _action_line(phase: str, do_next: str) -> str:
    """Name the phase and carry its immediate next action."""
    return f"Current phase: {phase}. Next: {do_next}"


def _skill_pointer_line(phase: str) -> str:
    """Point at the phase skill — explicitly a pointer, never a dependency."""
    return (
        f"Carry it out with the specflo '{phase_skill(phase)}' phase skill - a "
        "pointer only; the next action above stands on its own if the skill is "
        "unavailable."
    )


def _continue_line() -> str:
    """The clear-point plus both resume paths (REQ-11, D-02).

    Naming both paths is a wording choice, not a conditional: it keeps the CLI
    mode-agnostic (REQ-03) while still showing an agent in an auto run the
    command that re-emits its bootstrap.
    """
    return (
        f"{CLEAR_POINT_MARKER} - resume with `specflo checkpoint` "
        "(or `specflo auto` in an auto run)."
    )


def build_continuation(phase: str, do_next: str) -> str:
    """Render the continuation for ``phase`` with its derived ``do_next`` hint.

    Returns one contiguous block: the phase and its immediate next action, the
    phase-skill pointer, and the clear-point-and-continue line. Seams print their
    own transition line and checkpoint path around it; the block itself is what
    every seam shares.
    """
    return "\n".join([
        _action_line(phase, do_next),
        _skill_pointer_line(phase),
        _continue_line(),
    ])
