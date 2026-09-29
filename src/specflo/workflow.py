"""The specflo phase model (hardcoded for v0.1).

The workflow is a fixed linear sequence of phases. This is deliberately
hardcoded for now; if we ever need fork-able, per-project workflows we can
lift this into a YAML schema.
"""

PHASES: list[str] = ["brainstorm", "spec", "plan", "execute"]

# Quick level has one phase: the brief is written and worked in execute, and
# no approval sits between the two, so a phase boundary would do nothing.
# Harden level has the same one phase: its plan grows from review findings.
_QUICK_PHASES: list[str] = ["execute"]


def phases_for(level: str) -> list[str]:
    """The phases a project at ``level`` goes through, in order."""
    return _QUICK_PHASES if level in ("quick", "harden") else PHASES

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


def _whole_suite(test_command: str | None) -> str:
    """The words that ask for the whole test suite, naming the command when set."""
    if test_command:
        return f"run the whole test suite (`{test_command}`)"
    return "run the whole test suite"


# What completion needs once hardening stops, in a project whose gate reads
# the latest gate round's verdict and the items the rounds after it leave open.
_GATE_NEEDS = (
    "go on to completion, which needs a gate round whose verdict passes and no open item"
    " (`specflo validate execute` names what is left)"
)


def _harden_stop(
    quiet: list[str], test_command: str | None, finish: str, lead: str = "All tasks done - the"
) -> str:
    """The hint once the latest two harden rounds, ``quiet``, raised no new
    blocker or should-fix finding.

    It only suggests: stopping is the user's call. To stop, the whole suite
    runs once, then ``finish`` says how the project goes on to completion,
    which is what its gate still needs; to go on, another harden round opens.
    ``lead`` opens the sentence: a harden project, whose plan may be empty,
    names no tasks.
    """
    return (
        f"{lead} last two harden rounds, {quiet[0]} and {quiet[1]}, raised no"
        " new blocker or should-fix finding, so hardening may stop here; that is the user's"
        f" call. To stop, {_whole_suite(test_command)} once, then {finish}. To go on, open"
        " another harden round with `specflo review start --harden`."
    )


def _review_hint(review: dict | None, test_command: str | None = None) -> str:
    """What to do next once every task is done, given where the review stands.

    No round yet, a round left open, a round that passed, and a round that
    asked for changes each get their own next action; after a harden round,
    so do no gate round, items it left open, and items of the gate round a
    later round checked closed. Two quiet harden rounds in a
    row, the latest two rounds, get a suggestion to stop hardening in place
    of any of those.
    Only a passing round offers ``specflo advance``, and it reads the
    ``passing`` flag the review state carries rather than naming verdicts
    itself - so the hint and the completion gate can never disagree about
    which rounds clear it. The verdict only picks the words. The later keys
    (open items, the spent budget, earlier changes, the gate, the quiet
    rounds) are read with defaults, as a state dict may predate them. The hints that call for the
    whole suite name ``test_command`` when it is set.
    """
    if review is None:
        return (
            f"All tasks done - {_whole_suite(test_command)} once, then open the "
            "final review round with `specflo review start`, hand a fresh-context "
            "reviewer the brief `specflo review prompt` prints, and close the "
            "round with `specflo review done`."
        )
    if review["open"]:
        return (
            f"All tasks done - finish the open review round {review['file']}: run "
            "`specflo review start` to take it back (a round nobody wrote into yet "
            "takes HEAD), hand a fresh-context "
            "reviewer the brief `specflo review prompt` prints and have it record "
            "findings through the CLI; then close it with `specflo review done`."
        )
    # Only carried once a harden round is recorded: the run of harden rounds
    # with no new find that the series ends in.
    quiet = review.get("quiet_rounds") or []
    if len(quiet) >= 2:
        finish = (
            "run `specflo advance` to complete the project" if review["passing"]
            else _GATE_NEEDS
        )
        return _harden_stop(quiet[-2:], test_command, finish)
    if review["passing"]:
        # A round that asked for changes passes once every item it asks them
        # on is settled. It keeps its verdict, so the hint names the settling,
        # and the whole suite runs once more as after any such round.
        if review["verdict"] == "changes-requested":
            return (
                f"All tasks done and every item {review['file']} asks for changes on is "
                f"settled - {_whole_suite(test_command)} once more, then "
                "`specflo advance` to complete the project."
            )
        if review.get("after_changes"):
            return (
                f"All tasks done and {review['file']} is {review['verdict']} after a "
                f"round that asked for changes - {_whole_suite(test_command)} once "
                "more, then `specflo advance` to complete the project."
            )
        return (
            f"All tasks done and {review['file']} is {review['verdict']} - run "
            "`specflo advance` to complete the project."
        )
    # Only carried once a harden round is recorded: the latest gate round,
    # whose verdict the gate reads, and the items the rounds after it leave
    # open. Without it the latest round is the gate round.
    gate = review.get("gate")
    if gate is not None and gate["file"] is None:
        return (
            "All tasks done - no gate round is recorded, only harden rounds: "
            f"{_whole_suite(test_command)} once, then open a gate round with "
            "`specflo review start`, hand a fresh-context reviewer the brief "
            "`specflo review prompt` prints, and close the round with "
            "`specflo review done`."
        )
    if gate is not None and gate["passes"]:
        items = gate["left_open"]
        one = len(items) == 1
        return (
            f"All tasks done - {review['file']} leaves {', '.join(items)} open: fix "
            f"{'it' if one else 'each'} with a task that fixes it (`specflo task add "
            f"--fixes {items[0] if one else 'F-NN'}`), commit, then run "
            f"`specflo review start` for a round that checks the {'fix' if one else 'fixes'}."
        )
    name = review["file"] if gate is None else gate["file"]
    # Only carried once a later round checked closed an item the gate round
    # asks for changes on: its fix is proven, and only a new gate round
    # clears the verdict, so the hint names no fix for it.
    closed = (gate or {}).get("checked_closed") or []
    if closed:
        return _checked_closed_step(
            name, closed, review.get("open_items") or [], review.get("budget_spent", False)
        )
    if review.get("budget_spent"):
        return (
            f"All tasks done - {name} asks for changes and the level has "
            "used its review budget. The next step is the user's choice: one more "
            "round with `specflo review start --over-budget`, or waive the review "
            "with `specflo review waive --reason <why>`."
        )
    items = review.get("open_items") or []
    named = f" ({', '.join(items)})" if items else ""
    fixes = items[0] if len(items) == 1 else "F-NN"
    return (
        f"All tasks done - address the findings in {name}: fix each "
        f"blocker and should-fix item{named}, never the nits, with a task that "
        f"fixes it (`specflo task add --fixes {fixes}`), work it to done and "
        "commit, then run `specflo review start` for a round that checks the fixes."
    )


def _checked_closed_step(name: str, closed: list[str], items: list[str], spent: bool) -> str:
    """The hint once a later round checked closed ``closed``, items the
    latest gate round ``name`` asks for changes on that nobody settled.

    Their fix is proven, so only a new gate round clears the verdict; with
    no open item, ``items``, left, that round checks nothing and can pass.
    An open item is fixed as ever. ``spent`` says the level has used its
    review budget, so the gate round needs ``--over-budget`` unless the user
    waives the review.
    """
    one = len(closed) == 1
    proven = (
        f"{', '.join(closed)} {'was' if one else 'were'} checked closed by a later round, so"
        f" {'its fix is' if one else 'their fixes are'} proven, but only a new gate round"
        " clears the verdict"
    )
    lead = f"All tasks done - {name} asks for changes and"
    text = f"{lead} the level has used its review budget. {proven}" if spent else f"{lead} {proven}"
    checks = "nothing left and can pass"
    if items:
        single = len(items) == 1
        checks = f"the {'fix' if single else 'fixes'}"
        text += (
            f". {', '.join(items)} {'is' if single else 'are'} still open: fix"
            f" {'it' if single else 'each'} with a task that fixes it (`specflo task add"
            f" --fixes {items[0] if single else 'F-NN'}`), work {'it' if single else 'each'}"
            " to done and commit"
        )
    if spent:
        return (
            f"{text}. The next step is the user's choice: a gate round with `specflo review"
            f" start --over-budget`, which checks {checks}, or waive the review with"
            " `specflo review waive --reason <why>`."
        )
    then = ", then" if items else ":"
    return f"{text}{then} run `specflo review start` for a gate round, which checks {checks}."


_NONE_ACTIONABLE = (
    "Tasks remain but none are actionable - unblock or reopen one "
    "(`specflo task list`)."
)

# A harden project's move while no harden round has closed hardened.
_START_HARDEN = (
    "No harden round has closed hardened yet - fill in the brief first if `specflo validate"
    " brief` names an issue, then open a harden round with `specflo review start --harden`,"
    " hand a fresh-context reviewer the brief `specflo review prompt` prints, and close the"
    " round with `specflo review done`."
)


def _harden_step(
    progress: dict | None, review: dict | None, test_command: str | None = None
) -> str:
    """The hint a harden project gets at execute, where review findings grow the plan.

    No harden round closed hardened, a round left open, open items with no
    fix task, fix tasks not done, and open items whose fix tasks are done each
    get their own next move; so does an empty ledger after a harden round
    closed hardened, which completes the project. Two quiet harden rounds in
    a row, once no fix task waits, get the suggestion to stop in place of the
    last two. ``passing`` and ``unfixed`` are read from the review state, so
    the hint agrees with the completion gate and with ``review start``, which
    opens no round while an open item has no done fix task.
    """
    if review is None:
        return _START_HARDEN
    if review["open"]:
        return (
            f"Finish the open review round {review['file']}: run `specflo review start` to"
            " take it back (a round nobody wrote into yet takes HEAD), hand a fresh-context"
            " reviewer the brief `specflo review prompt` prints and have it record findings"
            " through the CLI; then close it with `specflo review done`."
        )
    reopened = review.get("reopened") or {}
    untasked = [
        item for item, tasks in (review.get("unfixed") or {}).items()
        if not tasks and item not in reopened
    ]
    if untasked:
        one = len(untasked) == 1
        return (
            f"{', '.join(untasked)} {'is' if one else 'are'} open with no fix task: add one"
            f"{'' if one else ' for each'} with `specflo task add --fixes"
            f" {untasked[0] if one else 'F-NN'}`, work {'it' if one else 'each'} to done and"
            " commit, then open the next harden round with `specflo review start --harden`"
            f" to check {'it' if one else 'them'}."
        )
    if progress is not None and progress.get("total", 0) > 0 and not progress.get("all_done"):
        actionable = progress.get("next_actionable") or []
        if not actionable:
            return _NONE_ACTIONABLE
        return (
            f"Work the next fix task: {', '.join(actionable)} (`specflo task show`); once"
            " every fix task is done and committed, open the next harden round with"
            " `specflo review start --harden` to check the fixes."
        )
    items = review.get("open_items") or []
    quiet = review.get("quiet_rounds") or []
    if len(quiet) >= 2:
        # Quiet rounds raise nothing new, but an item they checked open stays open.
        finish = "run `specflo advance` to complete the project"
        if items:
            one = len(items) == 1
            finish = (
                f"fix {', '.join(items)}, which {'is' if one else 'are'} still open, and have"
                f" a round opened with `specflo review start` check {'it' if one else 'them'}"
                f" closed, or reject or defer {'it' if one else 'each'}; completion needs no"
                " open item (`specflo validate execute` names what is left)"
            )
        return _harden_stop(quiet[-2:], test_command, finish, lead="The")
    if reopened:
        one = len(reopened) == 1
        named = ", ".join(f"{item} ({name})" for item, name in reopened.items())
        return (
            f"A round checked {named} open after {'its fix' if one else 'their fixes'}:"
            f" {'it needs' if one else 'each needs'} a new fix task. Add"
            f" {'one' if one else 'one for each'} with `specflo task add --fixes"
            f" {next(iter(reopened)) if one else 'F-NN'}`, work {'it' if one else 'each'} to"
            " done and commit, then open the next harden round with `specflo review start"
            f" --harden` to check {'it' if one else 'them'}."
        )
    if items:
        return (
            "Every open item has a done fix task - with the fixes committed, open the next"
            f" harden round with `specflo review start --harden` to check {', '.join(items)},"
            " hand a fresh-context reviewer the brief `specflo review prompt` prints, and"
            " close the round with `specflo review done`."
        )
    if review["passing"]:
        return (
            "No open item is left and a harden round closed hardened -"
            f" {_whole_suite(test_command)} once, then run `specflo advance` to complete the"
            " project."
        )
    # Closed, no item open and not passing: no harden round closed hardened.
    return _START_HARDEN


def _light_level_step(
    phase: str, validates: bool, level: str, unattended: bool = False
) -> str | None:
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
    if phase == "plan" and validates and unattended:
        # An auto run has no one to approve: it covers the one approval.
        return (
            "The plan validates. This auto run covers fast level's one approval:"
            " run `specflo advance` and go on to execute."
        )
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
    unattended: bool = False,
    test_command: str | None = None,
) -> str:
    """Return a human-readable hint for what to do while in ``phase``.

    ``shelved=True`` takes precedence over everything else (a paused project is
    not advanced from any phase): the hint directs to resume or start anew. For
    the ``execute`` phase the hint is otherwise progress-aware: pass the
    ``plan_progress`` dict and/or ``complete=True`` (project finished).

    ``review`` is the derived review state (``review.review_state``) or None
    when no round file exists; with every task done it decides which of the four
    review-aware hints is returned (review-rounds REQ-20). ``test_command`` is
    the configured one, named where those hints call for the whole suite. A
    harden project at execute takes its hint from ``review`` whatever its
    progress, as its plan starts empty and grows from findings.

    For brainstorm/spec/plan, ``validates=True`` means the phase's artifact
    passed its real validator, so the hint offers ``specflo advance`` and names
    the next phase instead of the static work hint (REQ-01). ``execute`` ignores
    ``validates`` and keeps its progress-based hint (REQ-05). With everything at
    its default the single-argument form is unchanged.
    """
    _require_known(phase)
    if not shelved and not complete:
        if level == "harden" and phase == "execute":
            return _harden_step(progress, review, test_command)
        light = _light_level_step(phase, validates, level, unattended)
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
                return _review_hint(review, test_command)
            actionable = progress.get("next_actionable") or []
            if actionable:
                return f"Work the next task: {', '.join(actionable)} (`specflo task show`)."
            return _NONE_ACTIONABLE
    elif validates:
        # brainstorm/spec/plan whose artifact passes its validator: derived
        # doneness -> offer the move rather than the work hint (REQ-01).
        return (
            f"The {phase} validates - run `specflo advance` to move to "
            f"the {next_phase(phase)} phase."
        )
    return _NEXT_STEP[phase]
