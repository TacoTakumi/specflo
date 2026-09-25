"""The specflo phase model (hardcoded for v0.1).

The workflow is a fixed linear sequence of phases. This is deliberately
hardcoded for now; if we ever need fork-able, per-project workflows we can
lift this into a YAML schema.
"""

PHASES: list[str] = ["brainstorm", "spec", "plan", "execute"]

# Quick level has one phase: the brief is written and worked in execute, and
# no approval sits between the two, so a phase boundary would do nothing.
_QUICK_PHASES: list[str] = ["execute"]


def phases_for(level: str) -> list[str]:
    """The phases a project at ``level`` goes through, in order."""
    return _QUICK_PHASES if level == "quick" else PHASES

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


def resolve_reopen_target(
    phase: str, target: str | None = None, level: str = "full"
) -> str:
    """Return the earlier phase ``reopen`` should move to, or raise ``ValueError``.

    ``reopen`` is the strict inverse of ``advance``: it only moves *backward*.

    - Bare (``target=None``): the immediately previous phase (undo the last
      advance). At the first phase this raises — there is nowhere earlier to go.
    - Named ``target``: must be an *earlier* phase than ``phase``. A target that
      is unknown, the current phase, or a later phase each raises a distinct
      ``ValueError`` (forward movement is ``specflo advance``).
    """
    _require_known(phase)
    phases = phases_for(level)
    current = phases.index(phase)
    if target is None:
        if current == 0:
            raise ValueError(
                f"Already at the first phase {phase!r}; there is nothing earlier "
                "to reopen."
            )
        return phases[current - 1]
    _require_known(target)
    if target not in phases:
        raise ValueError(
            f"A {level}-level project has no {target!r} phase to reopen."
        )
    dest = phases.index(target)
    if dest == current:
        raise ValueError(
            f"{target!r} is already the current phase; reopen moves to an earlier "
            "phase."
        )
    if dest > current:
        raise ValueError(
            f"{target!r} is later than the current phase {phase!r}; reopen only "
            "moves backward (use `specflo advance` to move forward)."
        )
    return target


def _review_hint(review: dict | None) -> str:
    """What to do next once every task is done, given where the review stands.

    Four states, four different next actions (review-rounds REQ-20): no round
    yet, a round left open, a round that passed, and a round that did not. Only
    the passing one offers ``specflo advance``, and it reads the ``passing`` flag
    the review state carries rather than naming verdicts itself - so the hint and
    the completion gate can never disagree about which verdicts clear it.
    """
    if review is None:
        return (
            "All tasks done - run the final whole-branch review (fresh context) "
            "and record it: `specflo review start`, then `specflo review done "
            "--verdict <v>`."
        )
    if review["open"]:
        return (
            f"All tasks done - finish the open review round {review['file']} and "
            "close it with `specflo review done --verdict <v>`."
        )
    if review["passing"]:
        return (
            f"All tasks done and {review['file']} is {review['verdict']} - run "
            "`specflo advance` to complete the project."
        )
    return (
        f"All tasks done - address the findings in {review['file']}, then run "
        "another round with `specflo review start`."
    )


def _light_level_step(phase: str, validates: bool, level: str) -> str | None:
    """The hint a quick or fast project gets in place of the full-level one.

    Quick works its brief in execute. Fast goes from brainstorm to the plan
    without stopping and stops once, when the plan validates, for the user's
    one approval before execute. None where the full-level hint applies.
    """
    if level == "quick" and phase == "execute":
        return (
            "Work the brief: set Goal and one Done when check, do the work, record"
            " Proof, then run `specflo advance`."
        )
    if level != "fast" or phase == "execute":
        return None
    if phase == "plan" and validates:
        return (
            "The plan validates. Fast level stops here once: show the user the brief"
            " (`specflo doc show brief`) and every choice they have not seen, and run"
            " `specflo advance` only after they approve."
        )
    if validates:
        return (
            f"The {phase} validates - run `specflo advance` and go on to the"
            f" {next_phase(phase)} phase without waiting for approval."
        )
    return (
        f"Fast level: write the {phase} yourself and keep going without waiting for"
        " approval; the one approval comes before execute."
    )


def next_step(
    phase: str,
    progress: dict | None = None,
    complete: bool = False,
    shelved: bool = False,
    validates: bool = False,
    review: dict | None = None,
    level: str = "full",
) -> str:
    """Return a human-readable hint for what to do while in ``phase``.

    ``shelved=True`` takes precedence over everything else (a paused project is
    not advanced from any phase): the hint directs to resume or start anew. For
    the ``execute`` phase the hint is otherwise progress-aware: pass the
    ``plan_progress`` dict and/or ``complete=True`` (project finished).

    ``review`` is the derived review state (``review.review_state``) or None
    when no round file exists; with every task done it decides which of the four
    review-aware hints is returned (review-rounds REQ-20).

    For brainstorm/spec/plan, ``validates=True`` means the phase's artifact
    passed its real validator, so the hint offers ``specflo advance`` and names
    the next phase instead of the static work hint (REQ-01). ``execute`` ignores
    ``validates`` and keeps its progress-based hint (REQ-05). With everything at
    its default the single-argument form is unchanged.
    """
    _require_known(phase)
    if not shelved and not complete:
        light = _light_level_step(phase, validates, level)
        if light is not None:
            return light
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
                return _review_hint(review)
            actionable = progress.get("next_actionable") or []
            if actionable:
                return f"Work the next task: {', '.join(actionable)} (`specflo task show`)."
            return (
                "Tasks remain but none are actionable - unblock or reopen one "
                "(`specflo task list`)."
            )
    elif validates:
        # brainstorm/spec/plan whose artifact passes its validator: derived
        # doneness -> offer the move rather than the work hint (REQ-01).
        return (
            f"The {phase} validates - run `specflo advance` to move to "
            f"the {next_phase(phase)} phase."
        )
    return _NEXT_STEP[phase]
