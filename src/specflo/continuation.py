"""The shared continuation prose: every seam's clear-point, every reseed directive.

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


# --- the reseed directives ----------------------------------------------
# `hook reseed` leads its payload with exactly one of these, chosen by the state
# of the active project and by whether the caller asked for a direct
# continuation. All four are continuation prose, so they live here: this module
# is the single producer (REQ-21 of pi-extension), and `hook.py` only selects.

# Leads the reseed payload so the agent surfaces state and asks rather than
# auto-running the checkpoint's "Do next" (continue-hook D-04). Source-neutral so
# it reads naturally for `startup`, `clear`, and `resume` alike. This is the
# *cold-start* directive: the caller is a session-start hook that cannot know
# whether the human wants to keep going.
CONFIRMATION_DIRECTIVE = (
    "You are resuming a specflo project. Do NOT begin work yet - the user may "
    "want to do something else, or not continue at all. Present the checkpoint "
    "below to the user and ask whether they want to continue, do something "
    "else, or stop, then wait for their answer."
)

# The counterpart for a caller that *already* decided to continue - it cleared
# context on purpose and wants the work carried on (pi-extension D-13, REQ-18).
# Re-asking there would waste the turn the clear just bought, so this directive
# is imperative and carries none of CONFIRMATION_DIRECTIVE's ask-first text.
# Selected by `hook reseed --continue`, never by the cold-start hook.
DIRECT_DIRECTIVE = (
    "Continue this specflo project now. Carry out the action under 'Do next' "
    "in the checkpoint below, then keep working the phase from there. Context "
    "was cleared deliberately and continuing is already decided, so there is "
    "nothing to confirm first."
)

# The complete-project directive: there is nothing to resume, so the agent must
# not "continue" the finished work — it surfaces completion and offers the next
# piece (`specflo new`) instead. Overrides both directives above.
COMPLETE_DIRECTIVE = (
    "The active specflo project is complete - there is nothing to resume. Do "
    "NOT begin work or pick the finished project back up. Tell the user the "
    "project is complete and ask whether they'd like to start a new project "
    "(`specflo new`) or do something else, then wait for their answer."
)

# The shelved-project directive: the project is paused, so the agent must not
# pick the work back up on its own — it surfaces the shelved state and offers
# resume *or* a new project. Overrides both in-flight directives above.
SHELVED_DIRECTIVE = (
    "The active specflo project is shelved (paused). Do NOT begin work or pick "
    "it back up on your own. Tell the user it is shelved and ask whether they'd "
    "like to resume it (`specflo resume`), start a new project (`specflo new`), "
    "or do something else, then wait for their answer."
)


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


def clear_point_only() -> str:
    """The clear-point-and-continue line alone, with no next-step hint.

    For a seam that knows it stands at a clear-point but could not derive what
    comes next: emitting the clear-point unconditionally beats falling silent,
    and keeps a seam's guarantee from regressing to best-effort (REQ-10). The
    wording still comes from here, so no seam holds a copy (REQ-04).
    """
    return _continue_line()


def _complete_line() -> str:
    """The terminal clear-point: a clear-point with *no* continue-instruction.

    Deliberately names neither resume command (REQ-07). A complete project has
    nothing to continue to, and naming the auto command here would invite an auto
    loop - which terminates on the CLI's completion signal - to start another
    pass over a finished project.
    """
    return f"{CLEAR_POINT_MARKER} - this project is complete."


def build_continuation(phase: str, do_next: str, complete: bool = False) -> str:
    """Render the continuation for ``phase`` with its derived ``do_next`` hint.

    Returns one contiguous block: the phase and its immediate next action, the
    phase-skill pointer, and the clear-point-and-continue line. Seams print their
    own transition line and checkpoint path around it; the block itself is what
    every seam shares.

    With ``complete=True`` (the project just finished) the block drops the
    continue-instruction entirely - no phase-skill pointer, and a clear-point
    naming neither resume command (REQ-07). It also drops the phase-and-next-action
    framing: a finished project has no current phase to be in and nothing next to
    do, so ``do_next`` (already a closing note, e.g. "Project complete. Start the
    next piece of work with `specflo new`.") stands on its own.
    """
    if complete:
        return "\n".join([do_next, _complete_line()])
    return "\n".join([
        _action_line(phase, do_next),
        _skill_pointer_line(phase),
        _continue_line(),
    ])
