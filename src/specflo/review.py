"""Review rounds: the ``review-N.md`` artifacts a project accrues.

A round is one end-of-execute review, minted from a skeleton by
``specflo review start`` and closed by ``specflo review done``. Round 1 reviews
the whole branch; a later round reviews only the diff since the latest reviewed
round's sha, unless ``--full`` asks for the whole branch again. Every piece of
review state - the round number, whether a round is open, its verdict, date and
sha - lives in these files and nowhere else (D-03), so the read path is a glob
over the project directory plus a frontmatter parse.

Numbers are allocated one above the highest file present, so a deleted round
leaves a permanent gap rather than a reused identity (D-07).
"""

from __future__ import annotations

import dataclasses
import datetime
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import yaml

from . import followup, markdown, plan
from .config import SpecfloConfig
from .errors import SpecfloError, require_one_line
from .locking import lock_path_for, locked
from .projects import load_project, project_dir

_ROUND_RE = re.compile(r"^review-(\d+)\.md$")
# A finding's severity. A blocker or a should-fix item asks for changes; a
# nit never does.
SEVERITIES = ("blocker", "should-fix", "nit")
FINDINGS_HEADER = "## Findings"
# Any F-NN in any round file counts for numbering, so a new ID can never
# repeat one a round already names, in a finding line or in its prose.
_ANY_FINDING_ID = re.compile(r"\bF-(\d+)\b")
# Where a finding's defect is: a file and a line, or a range of lines. A line
# number has no leading zero, so a parsed location renders back as written.
_LOCATION = r"(?P<path>[^\s\[\]]+):(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?"
# The mark a finding that breaks what once worked carries after its severity.
REGRESSION = "regression"
# One finding line, as `review finding add` writes it and as a reviewer with
# no shell may write it by hand: '- F-NN (severity) text', with an optional
# ', regression' inside the parentheses and an optional '[file:line]' or
# '[file:a-b]' before the text. A line with no location reads as it always did.
_FINDING_LINE = re.compile(
    r"^- (?P<id>F-(?P<number>\d+))"
    r" \((?P<severity>" + "|".join(map(re.escape, SEVERITIES)) + r")"
    r"(?P<regression>, " + REGRESSION + r")?\)"
    r" (?:\[" + _LOCATION + r"\] )?(?P<text>\S.*)$"
)
# The whole Findings section of a round that found nothing. A section with
# neither this nor a finding is refused, so a free-form report never passes
# as a clean one.
NONE_LINE = "- none"
# Where a round records whether each earlier blocker and should-fix item is
# closed, one '- F-NN closed' or '- F-NN open' line per item.
EARLIER_HEADER = "## Earlier findings"
CHECK_STATES = ("closed", "open")
_CHECK_LINE = re.compile(r"^- (F-(\d+)) (closed|open)$")
_CHECK_HOW = "`specflo review finding check F-NN closed|open`"
# Where the round that recorded a finding says it was settled without a fix,
# one line per finding: '- F-NN rejected: <reason>', or '- F-NN deferred FU-NN'
# naming the follow-up a later project picks it up from. A settled finding
# leaves the ledger. The section is appended to a closed round, so a round
# file with none reads as it always did.
SETTLED_HEADER = "## Settled"
REJECTED = "rejected"
DEFERRED = "deferred"
_SETTLED_LINE = re.compile(
    r"^- (?P<id>F-(?P<number>\d+)) (?:" + REJECTED + r": (?P<reason>\S.*)"
    r"|" + DEFERRED + r" (?P<followup>FU-\d+))$"
)
# Why a daemon-held project's finding is never deferred: follow-ups work only
# for projects in a checkout, and the refusal names the follow-up that will
# route a hosted project's.
_HOSTED_DEFER = (
    "Deferring a finding files a follow-up, and follow-ups for a hosted project"
    " are not routed yet (FU-90). Fix the finding with a task that names it in"
    " --fixes, or reject it with `specflo review finding reject F-NN --reason <why>`."
)
# What a refused close can do next, named in every refusal of its findings.
_WAYS_ON = (
    " To go on: rewrite the line in the '- F-NN (severity) [file:line] text'"
    " form, re-add the finding with `specflo review finding add`, or waive the"
    " round with `specflo review waive --reason <why>`."
)
# The severities whose finding must name where its defect is.
LOCATED = ("blocker", "should-fix")
# How `review finding add --at` spells a location, named in its refusals.
AT_FORM = "<file>:<line> or <file>:<line>-<line>"
# The line `git blame --porcelain` starts each blamed line with: the commit
# that last changed it, then the line's numbers. A content line starts with a
# tab, so it never matches.
_BLAME_COMMIT = re.compile(r"^([0-9a-f]{40,64}) \d+ \d+(?: \d+)?$", re.MULTILINE)
# The whole verdict vocabulary (D-06). ready-to-merge and waived pass the
# completion gate; changes-requested blocks it.
READY = "ready-to-merge"
CHANGES_REQUESTED = "changes-requested"
WAIVED = "waived"
VERDICTS = (READY, CHANGES_REQUESTED, WAIVED)
# The verdicts that clear the completion gate (D-08). ``waived`` is a
# deliberate, reasoned way past it, not an accident.
PASSING = (READY, WAIVED)
# A round's kind. A gate round is the review the round budget reads; a
# harden round is a fresh review of its whole scope that the budget skips.
# Only a harden round records its kind, so a round file with none - every
# file written before harden rounds - is a gate round.
GATE = "gate"
HARDEN = "harden"
KINDS = (GATE, HARDEN)
# Frontmatter key order, pinned so a close rewrites a round file in the
# shape `review start` minted it. A harden round's kind is minted after
# these, where a close keeps it (see :func:`_render`).
_FIELDS = ("round", "verdict", "date", "sha", "base", "level", "reason")
# Minting reads every round file then writes one; the lock covers that whole
# critical section so two processes cannot mint the same number. It is named
# for the series, not for a file, because the file's name is what is being
# decided inside it.
_LOCK_NAME = "review"

_TEMPLATE = """\
---
round: {number}
verdict: ''
date: '{today}'
sha: '{sha}'
base: '{base}'
level: '{level}'
reason: ''
{kind}---

# Review round {number}

## Scope reviewed

## Findings

## Verdict
"""


def number_of(path: Path) -> int | None:
    """The round number in a round file's name, or None if it is not one."""
    match = _ROUND_RE.match(Path(path).name)
    return int(match.group(1)) if match else None


def round_files(root: Path, cfg: SpecfloConfig, slug: str) -> list[tuple[int, Path]]:
    """Every round file in the project directory, ascending by number.

    Each path is carried alongside its number rather than rebuilt from it. A
    hand-created ``review-007.md`` parses as round 7, and reconstructing
    ``review-7.md`` from that number would name a file nobody wrote - which is
    how a single stray filename could otherwise take down every read path.
    """
    directory = project_dir(root, cfg, slug)
    if not directory.is_dir():
        return []
    found = [
        (number, entry)
        for entry in directory.iterdir()
        if entry.is_file() and (number := number_of(entry)) is not None
    ]
    return sorted(found, key=lambda item: (item[0], item[1].name))


def frontmatter(path: Path) -> dict:
    """A round file's frontmatter mapping; ``{}`` when it has none to read.

    A hand-mangled round file must not take the CLI down: whatever is wrong with
    it - no fence, unparseable YAML, or frontmatter that is not a mapping at all
    - it reads as a round with no verdict, which is the same as an open one, and
    the session is steered back into it rather than past it.
    """
    parts = path.read_text().split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        return {}
    try:
        fields = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    # A scalar or a list parses fine and is truthy, so `or {}` is not enough:
    # only a mapping has the keys the caller is about to ask for.
    return fields if isinstance(fields, dict) else {}


def round_kind(fields: dict) -> str:
    """A round's kind from its frontmatter mapping: ``harden`` when it says
    so, else ``gate`` - a round with no kind, or one a hand-edit spelled
    otherwise, is a gate round."""
    return HARDEN if fields.get("kind") == HARDEN else GATE


def open_round(root: Path, cfg: SpecfloConfig, slug: str) -> Path | None:
    """The open round's path, or None - when no round exists, or when the latest
    one carries a verdict.

    Derived from :func:`review_state`, never decided again here: a round is open
    exactly while the latest round - the highest-numbered one (REQ-19) - carries
    an empty verdict (REQ-03), and that judgement has one owner. An older
    unclosed file in a hand-edited directory is stale, not current, so it neither
    blocks the next mint nor gets reported as open. Two copies of the rule would
    be two chances for this and ``review_state`` to name different rounds as
    current - the disagreement derived state exists to prevent.

    The state carries the file's own name, so a hand-created ``review-007.md``
    is returned as itself rather than rebuilt as ``review-7.md``.
    """
    state = review_state(root, cfg, slug)
    if state is None or not state["open"]:
        return None
    return project_dir(root, cfg, slug) / state["file"]


def _reviewed_sha(rounds: list[tuple[int, Path]]) -> str:
    """The sha of the latest of ``rounds`` that was reviewed; "" when none was.

    A waived round reviewed nothing, so a range never starts at its sha: that
    would skip the fixes made before the waive.
    """
    for _, path in reversed(rounds):
        fields = frontmatter(path)
        if fields.get("verdict") in (READY, CHANGES_REQUESTED):
            return str(fields.get("sha", "") or "")
    return ""


def _first_reviewed_sha(rounds: list[tuple[int, Path]]) -> str | None:
    """The sha of the earliest of ``rounds`` that was reviewed; None when none was.

    A finding on a line changed after it is a regression. A waived round
    reviewed nothing, so it is never the first reviewed round. A reviewed
    round that records no sha gives "", which marks nothing.
    """
    for _, path in rounds:
        fields = frontmatter(path)
        if fields.get("verdict") in (READY, CHANGES_REQUESTED):
            return str(fields.get("sha", "") or "")
    return None


def budget(root: Path, cfg: SpecfloConfig, slug: str) -> dict:
    """The current level's review budget, read from the gate round files.

    A harden round is skipped: it neither counts nor is the latest round
    here. ``used`` counts the rounds whose level is the project's current
    level; a round with no level recorded counts toward it too. ``spent`` is
    True when the latest round asks for changes and the level has used every
    round ``review_max_rounds`` allows: the next round needs the user's say.
    ``regressions`` is the latest round's count (see :func:`regression_count`),
    None with no round; it changes neither ``used`` nor ``spent``.
    """
    level = load_project(root, cfg, slug).level
    files, rounds = [], []
    for number, path in round_files(root, cfg, slug):
        fields = frontmatter(path)
        if round_kind(fields) == GATE:
            files.append((number, path))
            rounds.append(fields)
    used = sum(1 for fields in rounds if (fields.get("level") or level) == level)
    latest = str(rounds[-1].get("verdict", "") or "") if rounds else ""
    limit = cfg.review_max_rounds
    return {
        "level": level,
        "used": used,
        "max": limit,
        "spent": latest == CHANGES_REQUESTED and used >= limit,
        "regressions": regression_count(files[-1][1]) if files else None,
    }


def budget_message(state: dict) -> str:
    """What to tell the user when the level's review budget is spent.

    The latest round's regressions are named when it has any.
    """
    count = state.get("regressions")
    marked = f" ({regressions_text(count)})" if count else ""
    return (
        f"The {state['level']} level has used its review budget ({state['used']} of"
        f" {state['max']} rounds) and the latest round asks for changes{marked}. Run one"
        " more round with `specflo review start --over-budget`, or waive the review with"
        " `specflo review waive --reason <why>`."
    )


def regressions_text(count: int) -> str:
    """``count`` regressions as a surface names them: '1 regression', '2 regressions'."""
    return f"{count} regression" + ("" if count == 1 else "s")


def regression_count(path: Path) -> int | None:
    """How many findings of the round at ``path`` carry the regression mark.

    None for a waived round, whose findings are never read. An open round
    counts the marks written so far. A line that is not a finding counts
    nothing: this reads the round and never refuses it.
    """
    if frontmatter(path).get("verdict") == WAIVED:
        return None
    return _marked_in(path.read_text())


def _marked_in(doc: str) -> int:
    """How many finding lines under a document's Findings heading carry the mark."""
    return sum(
        1 for line in _findings_lines(doc) or []
        if (finding := parse_finding_line(line)) and finding.regression
    )


def unfixed_items(root: Path, cfg: SpecfloConfig, slug: str) -> dict[str, list[str]]:
    """The open items no done task fixes, each with the tasks that fix it but are not done.

    An open item is a blocker or should-fix finding of a closed round that no
    reviewed round has checked closed and none has settled; a nit never is
    one. Only an active
    task whose progress is done counts as its fix: a superseded task fixes
    nothing, and a project with no plan has no fix at all. Reads the round
    files and plan.md and changes nothing.
    """
    rounds = round_files(root, cfg, slug)
    latest = rounds[-1][0] if rounds else 0
    items = _ledger(root, cfg, slug, latest + 1)[0]
    if not items:
        return {}
    return {
        item: [task.id for task in fixes]
        for item, fixes in _fix_tasks(root, cfg, slug, list(items.values())).items()
        if not any(task.progress == "done" for task in fixes)
    }


def _fix_tasks(
    root: Path, cfg: SpecfloConfig, slug: str, items: list[str]
) -> dict[str, list]:
    """The active tasks that fix each of ``items``, in plan order.

    A task's Fixes field names an item by its number. A superseded task
    fixes nothing, and a project with no plan has no fix at all. Reads
    plan.md and changes nothing.
    """
    try:
        tasks = plan.list_tasks(root, cfg, slug)
    except SpecfloError:
        tasks = []
    numbers = {int(item.split("-", 1)[1]): item for item in items}
    fixing: dict[str, list] = {item: [] for item in items}
    for task in tasks:
        for fix in task.fixes:
            match = re.fullmatch(r"F-(\d+)", fix.strip())
            if match and int(match.group(1)) in numbers:
                fixing[numbers[int(match.group(1))]].append(task)
    return fixing


def unfixed_message(unfixed: dict[str, list[str]]) -> str:
    """What to tell the user when an open item has no done fix task."""
    named = ", ".join(
        f"{item} ("
        + (f"{', '.join(tasks)} {'is' if len(tasks) == 1 else 'are'} not done" if tasks
           else "no fix task")
        + ")"
        for item, tasks in unfixed.items()
    )
    fixes = next(iter(unfixed)) if len(unfixed) == 1 else "F-NN"
    return (
        f"No review round opens while an open item has no done fix task: {named}."
        f" Each needs a task that fixes it, added with `specflo task add --fixes {fixes}`"
        " and finished with `specflo task done`; then run `specflo review start` again."
        " Or waive the review with `specflo review waive --reason <why>`."
    )


def start_round(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    today: str | None = None,
    full: bool = False,
    over_budget: bool = False,
    sha: str | None = None,
    need_fixes: bool = False,
    harden: bool = False,
) -> tuple[Path, bool]:
    """Mint the next round file, or hand back the open one (REQ-01..REQ-03).

    Returns ``(path, created)``; ``created`` is False when a round was already
    open, which is never an error - reusing it is how an abandoned review is
    resumed, and there is deliberately no way to discard one. A reused round
    keeps the scope it opened with.

    The new round records HEAD (the commit the reviewer reads), the project's
    level, and its base: the sha of the latest reviewed round, from which it
    reviews only the diff. ``full``, or no reviewed round with a sha, leaves
    the base empty and the round reviews the whole branch. ``sha`` is HEAD as
    the caller's checkout names it; None reads it from ``root``, which is only
    right where ``root`` is that checkout, never on a daemon.

    A reused round nobody has written into yet takes HEAD again: its review
    begins now. A ladder opens a level's round when it climbs, long before
    the level's work, and the next round's range must start at the commit
    the reviewer read, not at the climb.

    When the level's budget is spent (see :func:`budget`), no round opens
    unless ``over_budget`` says the user chose one more.

    ``need_fixes`` opens no round while an open item has no done fix task
    (see :func:`unfixed_items`), whatever ``full`` and ``over_budget`` say.
    The ``review start`` verb asks for it; a ladder's climb, which opens a
    level's round before the level's work, and a waive do not.

    ``harden`` opens a harden round, which records its kind: it always
    reviews its whole scope, so it has no base whatever ``full`` says, and
    the budget neither refuses it nor counts it, so ``over_budget`` changes
    nothing. It still needs each open item's fix, since it checks each item.
    It hands back an open harden round, and refuses an open gate round
    rather than pass it off as a harden one.
    """
    today = today or datetime.date.today().isoformat()
    directory = project_dir(root, cfg, slug)
    with locked(lock_path_for(root, slug, _LOCK_NAME)):
        existing = open_round(root, cfg, slug)
        if existing is not None:
            if harden and round_kind(frontmatter(existing)) != HARDEN:
                raise SpecfloError(_open_gate_message(existing.name))
            _restamp_untouched(root, existing, sha)
            return existing, False
        # Before the budget: --over-budget cannot open a round while an item
        # is unfixed, so the budget's choice is asked only once it could.
        if need_fixes and (unfixed := unfixed_items(root, cfg, slug)):
            raise SpecfloError(unfixed_message(unfixed))
        if not harden:
            state = budget(root, cfg, slug)
            if state["spent"] and not over_budget:
                raise SpecfloError(budget_message(state))
        rounds = round_files(root, cfg, slug)
        number = max((n for n, _ in rounds), default=0) + 1
        path = directory / f"review-{number}.md"
        path.write_text(_TEMPLATE.format(
            number=number,
            today=today,
            sha=head_sha(root) if sha is None else sha,
            base="" if full or harden else _reviewed_sha(rounds),
            level=load_project(root, cfg, slug).level,
            kind=f"kind: {HARDEN}\n" if harden else "",
        ))
    return path, True


def _open_gate_message(name: str) -> str:
    """What to tell the user who asks for a harden round while gate round ``name`` is open."""
    return (
        f"{name} is an open gate round, and a harden round opens only once it is"
        " closed. Close it with `specflo review done`, or waive it with"
        " `specflo review waive --reason <why>`; then run"
        " `specflo review start --harden` again."
    )


def _restamp_untouched(root: Path, path: Path, sha: str | None) -> None:
    """Stamp an open round nobody has written into with HEAD, if it moved."""
    fields = frontmatter(path)
    if not fields:
        return  # no frontmatter to read: rewriting it would empty round, base and level
    try:
        number = _round_number(path, fields.get("round"))
    except SpecfloError:
        return  # a hand-mangled round is its author's to fix; reuse it as it is
    if body_of(path).strip() != skeleton_body(number).strip():
        return
    head = head_sha(root) if sha is None else sha
    if head and head != str(fields.get("sha", "") or ""):
        fields["sha"] = head
        path.write_text(_render(fields, body_of(path)))


def skeleton_body(number: int) -> str:
    """The body ``review start`` mints for round ``number``."""
    minted = _TEMPLATE.format(number=number, today="", sha="", base="", level="", kind="")
    return minted.split("---", 2)[2].lstrip("\n")


def body_of(path: Path) -> str:
    """A round file's body: everything after its frontmatter."""
    parts = path.read_text().split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        return path.read_text()
    return parts[2].lstrip("\n")


def _render(fields: dict, body: str) -> str:
    """A round file from its frontmatter mapping and body.

    Keys keep their minted order; any other key - a harden round's kind, or
    one a human added - survives after them, so a gate round never gains a
    kind it was not minted with.
    """
    ordered = {key: fields.get(key, "") or "" for key in _FIELDS}
    ordered.update({k: v for k, v in fields.items() if k not in _FIELDS})
    frontmatter_text = yaml.safe_dump(ordered, sort_keys=False).strip()
    return f"---\n{frontmatter_text}\n---\n\n{body}"


def review_state(root: Path, cfg: SpecfloConfig, slug: str) -> dict | None:
    """The project's derived review state, or None while no round file exists.

    The latest round is the highest-numbered one, open or closed (REQ-19); its
    verdict is what every surface reports, so an open round after a passing one
    reads as open rather than as that earlier pass. ``passing`` says whether that
    round clears the completion gate - a passing verdict, or changes-requested
    with every item it asks them on settled (see :func:`_left_to_settle`) - so
    every reader shares one judgement. Read fresh from the files on every call -
    nothing is cached and nothing is mirrored (REQ-09).
    """
    files = round_files(root, cfg, slug)
    if not files:
        return None
    latest, path = files[-1]
    fields = frontmatter(path)
    verdict = str(fields.get("verdict", "") or "")
    passing = verdict in PASSING
    if verdict == CHANGES_REQUESTED:
        # The latest round is the gate round: it passes once every item it
        # asks for changes on is settled.
        blocking, left = _left_to_settle(root, cfg, slug, latest, path)
        passing = bool(blocking) and not left
    return {
        "rounds": len(files),
        "latest": latest,
        "verdict": verdict,
        "open": not verdict,
        # Whether this round clears the completion gate, decided here so the
        # next-step hint does not re-derive it. `workflow` cannot import this
        # module (review -> projects -> workflow would close a cycle), and two
        # copies of the rule are two chances to disagree.
        "passing": passing,
        "date": str(fields.get("date", "") or ""),
        "sha": str(fields.get("sha", "") or ""),
        "reason": str(fields.get("reason", "") or ""),
        "file": path.name,
        # How many of the round's findings carry the regression mark; None
        # for a waived round. A count only: the verdict never reads it.
        "regressions": regression_count(path),
        # What the next-step hint turns on after a close: the items the next
        # round must check, whether the level's round budget is spent, and
        # whether any earlier round asked for changes (fixes were made, so the
        # whole suite runs once more before completion).
        "open_items": items_to_check(root, cfg, slug, latest + 1),
        "budget_spent": budget(root, cfg, slug)["spent"],
        "after_changes": any(
            frontmatter(earlier).get("verdict") == CHANGES_REQUESTED
            for _, earlier in files[:-1]
        ),
    }


def completion_issues(root: Path, cfg: SpecfloConfig, slug: str) -> list[str]:
    """What the review still owes before the project may complete (REQ-21).

    Empty means the latest round is closed and passing, as
    :func:`review_state` decides it: a passing verdict, or asked for changes
    and every item it asks them on is settled, so settling the last items
    needs no further round. Past that the gate reads the verdict and nothing
    else, and never how old the round is (REQ-08). A reviewer that weighed
    some nits and still said ready-to-merge is not second-guessed here.
    """
    state = review_state(root, cfg, slug)
    if state is None:
        return [
            "no review round recorded: open one with `specflo review start`, hand the"
            " reviewer `specflo review prompt`, and close it with `specflo review done`."
        ]
    if state["open"]:
        return [
            f"review round {state['latest']} is still open ({state['file']}):"
            " close it with `specflo review done`."
        ]
    if not state["passing"]:
        if state["verdict"] == CHANGES_REQUESTED:
            # Name what still blocks once some of the round's items are settled.
            path = project_dir(root, cfg, slug) / state["file"]
            blocking, left = _left_to_settle(root, cfg, slug, state["latest"], path)
            if left != blocking:
                return [_still_blocks_message(state["file"], left)]
        return [
            f"the latest review round ({state['file']}) is {state['verdict']}:"
            " address the findings, then run another round with"
            " `specflo review start`."
        ]
    return []


def _still_blocks_message(name: str, items: list[str]) -> str:
    """What the gate says once some items round ``name`` asks for changes on
    are settled and ``items`` are not."""
    one = len(items) == 1
    finding_id = items[0] if one else "F-NN"
    return (
        f"the latest review round ({name}) is {CHANGES_REQUESTED} and {', '.join(items)}"
        f" still {'blocks' if one else 'block'} it: fix {'it' if one else 'them'}, then"
        " run another round with `specflo review start`, or settle"
        f" {'it' if one else 'each'} with `specflo review finding reject {finding_id}"
        f" --reason <why>` or `specflo review finding defer {finding_id} --do <what>`."
    )


def head_sha(root: Path) -> str:
    """The short HEAD sha, or "" wherever git cannot answer (REQ-07).

    Degrades exactly as ``index.py`` does: git missing, the directory untracked,
    a repo with no commits yet, or a hung call all give the empty stamp, and the
    close carries on. Nothing derives validity from this (REQ-08) - it is
    evidence for a human judgement call, never a verdict.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _round_number(path: Path, raw) -> int:
    """The number to write back into ``path``'s frontmatter.

    The filename is the authority: it is what allocated the round and what every
    read path counts, so a file whose frontmatter never carried a number keeps
    its own rather than being stamped 0. A frontmatter number that is not a
    number is a hand-edit the CLI will not guess at.
    """
    if raw is None or raw == "":
        return number_of(path) or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise SpecfloError(
            f"{path.name} has a non-numeric round ({raw!r}) in its frontmatter."
            " Fix it by hand, then close the round."
        ) from None


@dataclasses.dataclass(frozen=True)
class ClosedRound:
    """What a close decided: the round, its verdict, and its findings per severity.

    ``findings`` is None for a waived round, whose findings are never read.
    ``still_open`` names the earlier items the round checked open.
    ``regressions`` counts the findings that carry the regression mark once
    the round is closed; None for a waived round.
    """

    path: Path
    verdict: str
    findings: dict[str, int] | None
    still_open: list[str] = dataclasses.field(default_factory=list)
    regressions: int | None = None


@dataclasses.dataclass(frozen=True)
class Finding:
    """One finding line: its ID, severity and text, where it is, and its mark.

    ``path``, ``start`` and ``end`` are None for a line with no location;
    ``end`` is None too for a location of one line. ``regression`` is True
    when the severity carries the regression mark.
    """

    id: str
    severity: str
    text: str
    path: str | None = None
    start: int | None = None
    end: int | None = None
    regression: bool = False

    @property
    def number(self) -> int:
        """The NN of the finding's F-NN."""
        return int(self.id.split("-", 1)[1])

    @property
    def location(self) -> str | None:
        """``file:line`` or ``file:a-b``, or None for a line with no location."""
        if self.path is None:
            return None
        lines = f"{self.start}" if self.end is None else f"{self.start}-{self.end}"
        return f"{self.path}:{lines}"


def parse_finding_line(line: str) -> Finding | None:
    """The finding a line records, or None when it is not a finding line."""
    match = _FINDING_LINE.match(line)
    if match is None:
        return None
    start, end = match.group("start"), match.group("end")
    return Finding(
        id=match.group("id"),
        severity=match.group("severity"),
        text=match.group("text"),
        path=match.group("path"),
        start=int(start) if start else None,
        end=int(end) if end else None,
        regression=match.group("regression") is not None,
    )


def render_finding_line(finding: Finding) -> str:
    """The line that records ``finding``: what it was parsed from, byte for byte."""
    mark = f", {REGRESSION}" if finding.regression else ""
    where = f"[{finding.location}] " if finding.location else ""
    return f"- {finding.id} ({finding.severity}{mark}) {where}{finding.text}"


def _section_lines(doc: str, header: str) -> list[str] | None:
    """The non-blank lines under ``header``, comments dropped.

    None when the document has no such heading at all.
    """
    section = markdown.section_body(doc, header)
    if section is None:
        return None
    return [
        line.strip()
        for line in markdown.strip_comments(section).splitlines()
        if line.strip()
    ]


def _findings_lines(doc: str) -> list[str] | None:
    """The non-blank lines under a document's Findings heading, comments dropped."""
    return _section_lines(doc, FINDINGS_HEADER)


def _defined_numbers(doc: str) -> set[int]:
    """The number of each F-NN a finding line under the Findings heading defines."""
    return {
        finding.number
        for line in _findings_lines(doc) or []
        if (finding := parse_finding_line(line))
    }


def parse_findings(
    root: Path, cfg: SpecfloConfig, slug: str, path: Path, body: str
) -> list[Finding]:
    """The round's findings, each with its location and mark, or a refusal.

    Every line under Findings is a finding line, or the section is exactly
    ``- none``. Refused: a line in neither form, an F-NN that this round or
    another round of the project already defines, a section with neither,
    and ``- none`` beside findings. Each refusal names the line or the ID.
    """
    lines = _findings_lines(body)
    if not lines:
        raise SpecfloError(
            f"{path.name} records no findings and no '{NONE_LINE}' under"
            f" '{FINDINGS_HEADER}'; a clean round says '{NONE_LINE}' there."
            + _WAYS_ON
        )
    if set(lines) == {NONE_LINE}:
        return []
    if NONE_LINE in lines:
        raise SpecfloError(
            f"{path.name} says '{NONE_LINE}' under '{FINDINGS_HEADER}' and also"
            " records findings." + _WAYS_ON
        )
    elsewhere = {
        number
        for _, other in round_files(root, cfg, slug)
        if other != path
        for number in _defined_numbers(other.read_text())
    }
    seen: set[int] = set()
    findings = []
    for line in lines:
        finding = parse_finding_line(line)
        if finding is None:
            raise SpecfloError(
                f"{path.name} has a line under '{FINDINGS_HEADER}' that is not a"
                f" finding: {line!r}." + _WAYS_ON
            )
        if finding.number in seen or finding.number in elsewhere:
            where = (
                "earlier in this round" if finding.number in seen
                else "in another round of the project"
            )
            raise SpecfloError(
                f"{path.name} records {finding.id}, an ID already used {where}."
                + _WAYS_ON
            )
        seen.add(finding.number)
        findings.append(finding)
    return findings


def _check_locations(path: Path, findings: list[Finding]) -> None:
    """Refuse a blocker or should-fix finding with no location, naming its line.

    Asked only as the open round closes: a round closed before a finding
    named its location is never refused for lacking one.
    """
    for finding in findings:
        if finding.severity in LOCATED and finding.path is None:
            raise SpecfloError(
                f"{path.name} records a {finding.severity} with no location:"
                f" {render_finding_line(finding)!r}. A blocker or should-fix names"
                " where its defect is, as the file is at the round's sha." + _WAYS_ON
            )


def _mark_regressions(
    path: Path, body: str, findings: list[Finding], ids: list[str], first: str | None
) -> str:
    """``body`` with each finding ``ids`` names marked a regression, or a refusal.

    The caller told the marks in its own checkout; the round is trusted for
    the rest. Refused: an ID that is not a finding of the round, a nit, and
    any mark when no round before this one was reviewed (``first`` None). A
    blocker or should-fix with no location never gets here: the close
    refuses it first. A line already marked keeps its mark, and every byte
    outside the marked lines is kept.
    """
    if first is None:
        raise SpecfloError(
            "A finding is marked as a regression only after a reviewed round,"
            f" and no round before {path.name} was reviewed."
        )
    recorded = {finding.id: finding for finding in findings}
    for finding_id in ids:
        if finding_id not in recorded:
            raise SpecfloError(
                f"{finding_id} is not a finding of {path.name}, so it cannot be"
                " marked as a regression."
            )
        if recorded[finding_id].severity not in LOCATED:
            raise SpecfloError(
                f"A {recorded[finding_id].severity} is never marked as a regression."
            )
    lines = body.splitlines(keepends=True)
    start = next(
        index for index, line, in_fence in markdown.iter_lines_with_fence(body)
        if not in_fence and line.strip() == FINDINGS_HEADER
    )
    section = markdown.section_body(body, FINDINGS_HEADER) or ""
    for index in range(start + 1, start + 1 + len(section.splitlines(keepends=True))):
        written = lines[index].strip()
        finding = parse_finding_line(written)
        if finding is None or finding.regression or finding.id not in ids:
            continue
        # The line the close parsed, not a stale copy of its ID in a comment.
        target = recorded[finding.id]
        if (finding.severity, finding.location) == (target.severity, target.location):
            lines[index] = lines[index].replace(
                written, render_finding_line(dataclasses.replace(finding, regression=True)), 1
            )
    return "".join(lines)


def derive_verdict(
    findings: list[Finding], still_open: list[str] | None = None
) -> tuple[str, dict[str, int]]:
    """The verdict a round gives, and how many findings it has per severity.

    Any blocker or should-fix finding, or any earlier item checked open, asks
    for changes; nits never do.
    """
    counts = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        counts[finding.severity] += 1
    blocking = counts["blocker"] + counts["should-fix"] + len(still_open or [])
    return (CHANGES_REQUESTED if blocking else READY), counts


def parse_settled(doc: str) -> dict[int, tuple[str, str]]:
    """Each finding a document's Settled section settles, keyed by its number:
    how it was settled and the rest of its line, a rejection's reason or a
    deferral's FU-NN.

    A line in another form settles nothing: this reads a closed round and
    never refuses it.
    """
    return {
        int(match.group("number")): (
            (REJECTED, match.group("reason")) if match.group("reason") is not None
            else (DEFERRED, match.group("followup"))
        )
        for line in _section_lines(doc, SETTLED_HEADER) or []
        if (match := _SETTLED_LINE.match(line))
    }


def _ledger(
    root: Path, cfg: SpecfloConfig, slug: str, number: int
) -> tuple[
    dict[int, str], dict[int, tuple[str, str]], dict[int, str], dict[int, tuple[str, str]]
]:
    """What the rounds before round ``number`` leave for it to check.

    Returns ``(items, known, closed, settled)``, each keyed by the finding's
    number: the blocker and should-fix items no reviewed round has checked
    closed and none has settled, every finding recorded (ID and severity),
    the round file where each closed item was checked closed, and how each
    settled finding was settled and the round file that says so. A waived
    round's checks do not close an item: a waive reviewed nothing.
    """
    items: dict[int, str] = {}
    known: dict[int, tuple[str, str]] = {}
    closed: dict[int, str] = {}
    settled: dict[int, tuple[str, str]] = {}
    for n, path in round_files(root, cfg, slug):
        if n >= number:
            break
        doc = path.read_text()
        if frontmatter(path).get("verdict") in (READY, CHANGES_REQUESTED):
            for line in _section_lines(doc, EARLIER_HEADER) or []:
                match = _CHECK_LINE.match(line)
                if match and match.group(3) == "closed":
                    items.pop(int(match.group(2)), None)
                    closed[int(match.group(2))] = path.name
        for line in _findings_lines(doc) or []:
            finding = parse_finding_line(line)
            if finding:
                known[finding.number] = (finding.id, finding.severity)
                if finding.severity != "nit":
                    items[finding.number] = finding.id
        for key, (kind, _) in parse_settled(doc).items():
            settled[key] = (kind, path.name)
    for key in settled:
        items.pop(key, None)
    return items, known, closed, settled


def _blocking_items(doc: str) -> dict[int, str]:
    """The items a closed round asks for changes on, keyed by number: its own
    blocker and should-fix findings and the earlier items it checked open.

    A nit never blocks.
    """
    items: dict[int, str] = {}
    for line in _findings_lines(doc) or []:
        finding = parse_finding_line(line)
        if finding and finding.severity != "nit":
            items[finding.number] = finding.id
    for line in _section_lines(doc, EARLIER_HEADER) or []:
        match = _CHECK_LINE.match(line)
        if match and match.group(3) == "open":
            items[int(match.group(2))] = match.group(1)
    return dict(sorted(items.items()))


def _left_to_settle(
    root: Path, cfg: SpecfloConfig, slug: str, number: int, path: Path
) -> tuple[list[str], list[str]]:
    """The items round ``number`` at ``path`` asks for changes on, and those
    of them not settled yet.

    A finding the round raised blocks until it is settled, whether a task
    fixed it or not: only a later round checks a fix closed.
    """
    blocking = _blocking_items(path.read_text())
    settled = _ledger(root, cfg, slug, number + 1)[3]
    return (
        list(blocking.values()),
        [finding_id for key, finding_id in blocking.items() if key not in settled],
    )


def parse_checks(
    root: Path, cfg: SpecfloConfig, slug: str, path: Path, body: str, number: int
) -> list[str]:
    """The earlier items the round checked open, or a refusal.

    Every line under Earlier findings is a check of an item the round must
    check, and every such item has one. Refused: a line not in the
    '- F-NN closed|open' form, a check of anything else, and an item with
    no check. Each refusal names the line or the IDs.
    """
    items = _ledger(root, cfg, slug, number)[0]
    states: dict[int, str] = {}
    for line in _section_lines(body, EARLIER_HEADER) or []:
        match = _CHECK_LINE.match(line)
        if match is None:
            raise SpecfloError(
                f"{path.name} has a line under '{EARLIER_HEADER}' that is not a"
                f" check: {line!r}. Write it as '- F-NN closed' or '- F-NN open',"
                f" or record it with {_CHECK_HOW}."
            )
        if int(match.group(2)) not in items:
            raise SpecfloError(
                f"{path.name} checks {match.group(1)}, which is not an item this"
                " round checks. Items: " + (", ".join(items.values()) or "none") + "."
            )
        states[int(match.group(2))] = match.group(3)
    missing = [finding_id for key, finding_id in items.items() if key not in states]
    if missing:
        raise SpecfloError(
            f"{path.name} has not checked {', '.join(missing)}. Check each earlier"
            f" item with {_CHECK_HOW} before closing the round."
        )
    return [items[key] for key, state in states.items() if state == "open"]


def _nits_title(number: int) -> str:
    """The title of the follow-up round ``number`` files for its nits as it
    closes. With the round file alone as its From line, it tells that
    follow-up from one a reviewer recorded from the round."""
    return f"Nits from review round {number}"


def close_round(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    verdict: str | None = None,
    reason: str | None = None,
    today: str | None = None,
    report_text: str | None = None,
    sha: str | None = None,
    nits_followup: bool = True,
    regressions: list[str] | None = None,
) -> ClosedRound:
    """Close the open round with the verdict its findings give, date and sha.

    The verdict is derived from the round's Findings section (see
    :func:`parse_findings` and :func:`derive_verdict`). An explicit
    ``verdict`` is accepted only when it is the derived one, except
    ``waived``, which closes the round without reading its findings.

    Every blocker and should-fix finding names its location, whether
    ``review finding add`` wrote the line or a reviewer did by hand.
    ``regressions`` names the findings to mark as regressions as the round
    closes: the caller blames their lines in its own checkout (see
    :func:`regression_marks`), since a daemon holds no code, and the marks
    are written as sent. A line already marked keeps its mark. The closed
    round counts its marked findings, and the verdict is the one it would be
    with no mark.

    A round with nits adds one follow-up naming their IDs, so the nits stay
    listed after the project completes without blocking it. ``nits_followup``
    False keeps them in the round file only: follow-ups work only for
    projects in a checkout, so a daemon-held round adds none.

    ``sha`` is HEAD as the caller's checkout names it, used only when the
    round has no sha yet; None reads it from ``root``.

    The date stamps the close, overwriting the mint-time date: what matters
    is when the review was decided, not when its file appeared. The sha stays
    the one stamped when the round opened.

    ``report_text`` becomes the round's body (REQ-10) - the escape hatch for
    a reviewer that returns its report as text rather than writing into the
    file. The caller reads the report file itself and passes the text, so
    this never opens a file the caller named. It is refused once the body
    has been written into, so an ingest can never overwrite a review someone
    already recorded.

    Raises ``SpecfloError`` - leaving every file untouched - when the verdict is
    not one of :data:`VERDICTS`, when ``waived`` comes without a reason
    (REQ-06), when the body is already written, when no round is open, when
    the findings are malformed, when a blocker or should-fix names no
    location, when an explicit verdict is not the derived one, or on a mark
    :func:`_mark_regressions` refuses.
    """
    if verdict is not None and verdict not in VERDICTS:
        raise SpecfloError(
            f"Unknown verdict {verdict!r}. Valid values: " + ", ".join(VERDICTS) + "."
        )
    if verdict == WAIVED and not (reason or "").strip():
        raise SpecfloError(
            "Verdict 'waived' needs a --reason, so a project that skipped review"
            " records why."
        )
    if verdict == WAIVED and regressions:
        raise SpecfloError(
            "A waived round closes without reading its findings, so none of them"
            " is marked as a regression."
        )
    with locked(lock_path_for(root, slug, _LOCK_NAME)):
        path = open_round(root, cfg, slug)
        if path is None:
            raise SpecfloError(
                "No review is open. Start one with `specflo review start`."
            )
        fields = frontmatter(path)
        fields["round"] = _round_number(path, fields.get("round"))
        body = body_of(path)
        if report_text is not None:
            if body.strip() != skeleton_body(int(fields.get("round") or 0)).strip():
                raise SpecfloError(
                    f"{path.name} already has content; ingesting a report would"
                    " overwrite it. Close the round without --file instead."
                )
            body = report_text
        counts = None
        marked = None
        still_open: list[str] = []
        if verdict != WAIVED:
            findings = parse_findings(root, cfg, slug, path, body)
            _check_locations(path, findings)
            if regressions:
                earlier = [(n, p) for n, p in round_files(root, cfg, slug) if p != path]
                body = _mark_regressions(
                    path, body, findings, regressions, _first_reviewed_sha(earlier)
                )
            # Counted once the marks are written; the verdict never reads them.
            marked = _marked_in(body)
            still_open = parse_checks(root, cfg, slug, path, body, fields["round"])
            derived, counts = derive_verdict(findings, still_open)
            if verdict is not None and verdict != derived:
                blocking = [f.id for f in findings if f.severity != "nit"]
                blocking += still_open
                raise SpecfloError(
                    f"The findings in {path.name} make it {derived}"
                    + (f" ({', '.join(blocking)})" if blocking else "")
                    + f", not {verdict}. Run `specflo review done` without"
                    " --verdict, or waive the round with `--verdict waived"
                    " --reason <why>`."
                )
            verdict = derived
            nits = [f.id for f in findings if f.severity == "nit"]
            if nits and nits_followup:
                # Before the round is written: a follow-up that cannot be
                # added refuses the close rather than losing the nits.
                followup.add_followup(
                    root, cfg, slug,
                    _nits_title(fields["round"]),
                    f"Decide which of {', '.join(nits)} to fix",
                    source=path.name,
                    today=today,
                )
        fields["verdict"] = verdict
        fields["date"] = today or datetime.date.today().isoformat()
        # The sha the round opened at is the commit its reviewer read; only a
        # round that never got one takes HEAD now.
        if not fields.get("sha"):
            fields["sha"] = head_sha(root) if sha is None else sha
        if reason is not None:
            fields["reason"] = reason
        path.write_text(_render(fields, body))
    return ClosedRound(
        path=path, verdict=verdict, findings=counts, still_open=still_open, regressions=marked
    )


def _next_finding_id(root: Path, cfg: SpecfloConfig, slug: str) -> str:
    """One above the highest F-NN any round file of the project names."""
    numbers = [
        int(match.group(1))
        for _, path in round_files(root, cfg, slug)
        for match in _ANY_FINDING_ID.finditer(path.read_text())
    ]
    return f"F-{max(numbers, default=0) + 1:02d}"


def parse_location(at: str) -> tuple[str, int, int | None]:
    """The file and lines a location names: ``(path, start, end)``, or a refusal.

    ``end`` is None for one line. Refused: anything not in the form
    ``file:line`` or ``file:a-b``, and a range that ends before it starts.
    Reads nothing: whether the file holds those lines is
    :func:`check_location`'s question.
    """
    match = re.fullmatch(_LOCATION, at)
    if match is None:
        raise SpecfloError(
            f"--at {at!r} is not a location. Write it as {AT_FORM}, such as"
            " src/app.py:10 or src/app.py:9-12: a line starts at 1, and the file"
            " has no spaces or brackets in its name."
        )
    path, start = match.group("path"), int(match.group("start"))
    end = int(match.group("end")) if match.group("end") else None
    if end is not None and end < start:
        raise SpecfloError(
            f"--at {at}: the range ends before it starts. Write the lower line"
            f" first: {path}:{end}-{start}."
        )
    return path, start, end


def validate_finding(
    severity: str, text: str, location: str | None, regression: bool = False,
) -> tuple[str, int, int | None] | None:
    """Refuse a finding :func:`add_finding` would refuse before it reads a file.

    Returns the parsed location, or None for a finding with none. Raises
    ``SpecfloError`` on a severity outside :data:`SEVERITIES`, a nit marked
    as a regression, an empty or multi-line text, a blocker or should-fix
    with no location, and a location :func:`parse_location` refuses. The CLI
    asks first, so a bad argument is named before it checks the location in
    its checkout.
    """
    if severity not in SEVERITIES:
        raise SpecfloError(
            f"Unknown severity {severity!r}. Valid values: " + ", ".join(SEVERITIES) + "."
        )
    if regression and severity not in LOCATED:
        raise SpecfloError(f"A {severity} is never marked as a regression.")
    if not text.strip():
        raise SpecfloError("A finding needs a non-empty --text.")
    # Any line break the round file is later split on, not only \n and \r.
    if text.splitlines() != [text]:
        raise SpecfloError("A finding's --text must be one line.")
    if location is None:
        if severity in LOCATED:
            raise SpecfloError(
                f"A {severity} finding needs --at {AT_FORM}: where its defect is,"
                " as the file is at the round's sha. Only a nit may leave it out."
            )
        return None
    return parse_location(location)


def check_location(
    root: Path, sha: str, path: str, start: int, end: int | None
) -> str | None:
    """Refuse a location whose lines are not inside a file at ``sha``.

    Runs git in the checkout at ``root``, which must be the caller's: a
    daemon holds only the documents, so this never runs in a service.
    ``path`` is as git names it at ``sha``, from the repository root.

    Returns None once the location is checked, or why it could not be
    checked: the round records no sha or one that is not a commit id, or git
    here cannot read that commit (git missing, no repository, or a commit
    not fetched). Such a location is
    not refused - a reviewer the checkout cannot answer for still records it.
    Raises ``SpecfloError`` when ``path`` is not a file at ``sha`` or a line
    lies past its end.
    """
    if not sha:
        return "the round records no sha"
    # The sha may come from a daemon: only a commit id goes into git's argv.
    if not re.fullmatch(r"[0-9a-f]{4,64}", sha):
        return f"the round's sha {sha!r} is not a commit id"
    try:
        commit = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=root, capture_output=True, timeout=30,
        )
        if commit.returncode != 0:
            return f"this checkout has no commit {sha}"
        blob = subprocess.run(
            ["git", "cat-file", "blob", f"{sha}:{path}"],
            cwd=root, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return f"git cannot read commit {sha} here"
    shown = f"{path}:{start}" if end is None else f"{path}:{start}-{end}"
    if blob.returncode != 0:
        raise SpecfloError(
            f"--at {shown}: {path} is not a file at the round's sha {sha}. Name the"
            " file as it is at that commit, from the repository root."
        )
    data = blob.stdout
    count = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    last = start if end is None else end
    if last > count:
        lines = "line" if count == 1 else "lines"
        raise SpecfloError(
            f"--at {shown}: {path} has {count} {lines} at the round's sha {sha},"
            f" so line {last} is past its end."
        )
    return None


def regression_mark(
    root: Path, sha: str, first: str | None, severity: str,
    path: str | None, start: int | None, end: int | None,
) -> tuple[bool, str | None]:
    """Whether a finding is a regression, and why that could not be told.

    A blocker or should-fix finding is one when a line of its location,
    blamed at ``sha`` (the round's), was last changed by a commit that
    ``first`` - the sha of the project's first reviewed round - does not
    contain: the commit is neither ``first`` nor an ancestor of it. ``first``
    is None when no earlier round was reviewed. Such a finding, a nit and a
    finding with no location are never marked, and git does not run.

    Runs git in the checkout at ``root``, which must be the caller's, as
    :func:`check_location` does. Returns ``(marked, None)`` once told, or
    ``(False, why)`` when it could not be: a sha missing or not a commit id,
    or git here unable to read a commit or blame the file. Such a finding
    is not marked.
    """
    if severity not in LOCATED or first is None or path is None:
        return False, None
    for name, value in (("the round", sha), ("the first reviewed round", first)):
        if not value:
            return False, f"{name} records no sha"
        # A sha may come from a daemon: only a commit id goes into git's argv.
        if not re.fullmatch(r"[0-9a-f]{4,64}", value):
            return False, f"{name}'s sha {value!r} is not a commit id"
    lines = f"{start},{start if end is None else end}"
    try:
        for commit in (sha, first):
            found = subprocess.run(
                ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                cwd=root, capture_output=True, timeout=30,
            )
            if found.returncode != 0:
                return False, f"this checkout has no commit {commit}"
        blame = subprocess.run(
            ["git", "blame", "--porcelain", "-L", lines, sha, "--", path],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        if blame.returncode != 0:
            return False, f"git cannot blame {path} at {sha} here"
        unknown = None
        for commit in sorted(set(_BLAME_COMMIT.findall(blame.stdout))):
            # Exit 0: contained in first; 1: not, so changed after it.
            ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit, first],
                cwd=root, capture_output=True, timeout=30,
            )
            if ancestor.returncode == 1:
                return True, None
            if ancestor.returncode != 0:
                unknown = f"git cannot tell whether commit {commit} is in {first}"
    except (OSError, subprocess.TimeoutExpired):
        return False, f"git cannot blame {path} at {sha} here"
    return False, unknown


def regression_marks(
    root: Path, sha: str, first: str | None, body: str
) -> tuple[list[str], list[tuple[Finding, str]]]:
    """The findings of a round's text to mark as regressions, and those not told.

    ``body`` is what the round closes with: the round file, or the report
    ``review done --file`` ingests. Each finding line under its Findings
    heading with no mark yet is told as :func:`regression_mark` tells one,
    in the caller's checkout at ``root``; a line already marked keeps its
    mark and is not blamed. A line that is not a finding is the close's to
    refuse. Returns the IDs to mark, and each finding the checkout could not
    tell with why.
    """
    marks: list[str] = []
    untold: list[tuple[Finding, str]] = []
    for line in _findings_lines(body) or []:
        finding = parse_finding_line(line)
        if finding is None or finding.regression:
            continue
        marked, why = regression_mark(
            root, sha, first, finding.severity, finding.path, finding.start, finding.end
        )
        if marked:
            marks.append(finding.id)
        elif why:
            untold.append((finding, why))
    return marks, untold


def add_finding(
    root: Path, cfg: SpecfloConfig, slug: str, severity: str, text: str,
    location: str | None = None, regression: bool = False,
) -> tuple[str, Path]:
    """Append ``- F-NN (severity) [location] text`` to the open round's Findings section.

    Returns ``(F-NN, round path)``. The ID is numbered across every round of
    the project, and the lock spans the read that picks it and the write that
    records it, so two adds at once never share an ID.

    ``location`` is ``file:line`` or ``file:a-b``, written as sent: the caller
    checks it against the round's sha in its own checkout (see
    :func:`check_location`), since a daemon holds no code. A nit may have none.
    ``regression`` marks the finding a regression, also as sent: the caller
    blames its lines there (see :func:`regression_mark`).

    Raises ``SpecfloError`` - leaving every file untouched - on anything
    :func:`validate_finding` refuses, no open round, a regression mark with
    no reviewed round before the open one, or an open round with no Findings
    section.
    """
    where = validate_finding(severity, text, location, regression)
    path_, start, end = where or (None, None, None)
    with locked(lock_path_for(root, slug, _LOCK_NAME)):
        path = open_round(root, cfg, slug)
        if path is None:
            raise SpecfloError(
                "No review is open. Start one with `specflo review start`."
            )
        earlier = [(n, p) for n, p in round_files(root, cfg, slug) if p != path]
        if regression and _first_reviewed_sha(earlier) is None:
            raise SpecfloError(
                "A finding is marked as a regression only after a reviewed round,"
                f" and no round before {path.name} was reviewed."
            )
        doc = path.read_text()
        body = markdown.section_body(doc, FINDINGS_HEADER)
        if body is None:
            raise SpecfloError(
                f"{path.name} has no '{FINDINGS_HEADER}' section. Add the heading"
                " by hand, then add the finding again."
            )
        finding_id = _next_finding_id(root, cfg, slug)
        line = render_finding_line(
            Finding(finding_id, severity, text.strip(), path_, start, end, regression)
        )
        kept = body.strip("\n")
        path.write_text(
            markdown.replace_section_body(
                doc, FINDINGS_HEADER, f"{kept}\n{line}" if kept else line
            )
        )
    return finding_id, path


WHOLE_BRANCH = "whole-branch"
DELTA = "delta"


def items_to_check(root: Path, cfg: SpecfloConfig, slug: str, number: int) -> list[str]:
    """The blocker and should-fix F-NN round ``number`` must check: those the
    rounds before it recorded, no reviewed round has checked closed and none
    has settled."""
    return list(_ledger(root, cfg, slug, number)[0].values())


def review_scope(root: Path, cfg: SpecfloConfig, slug: str) -> dict:
    """What the open round reviews: its kind, its scope, its range, and the items to check.

    A round with a base reviews the delta ``<base>..HEAD``; one without
    reviews the whole branch. ``sha`` is the commit the round opened at,
    which a caller checks a finding's location against in its own checkout.
    ``first_reviewed_sha`` is the sha of the project's first reviewed round,
    None when no earlier round was reviewed: a caller marks a finding on a
    line changed after it a regression (see :func:`regression_mark`).
    Reads the round files and changes nothing. Raises ``SpecfloError`` when
    no round is open.
    """
    path = open_round(root, cfg, slug)
    if path is None:
        raise SpecfloError("No review is open. Start one with `specflo review start`.")
    fields = frontmatter(path)
    number = _round_number(path, fields.get("round"))
    base = str(fields.get("base", "") or "")
    earlier = [(n, p) for n, p in round_files(root, cfg, slug) if p != path]
    return {
        "round": number,
        "file": path.name,
        "sha": str(fields.get("sha", "") or ""),
        "first_reviewed_sha": _first_reviewed_sha(earlier),
        "kind": round_kind(fields),
        "scope": DELTA if base else WHOLE_BRANCH,
        "base": base,
        "range": f"{base}..HEAD" if base else None,
        "items": items_to_check(root, cfg, slug, number),
    }


def check_finding(
    root: Path, cfg: SpecfloConfig, slug: str, finding_id: str, state: str
) -> tuple[str, Path]:
    """Write '- F-NN closed|open' under the open round's Earlier findings.

    Returns ``(F-NN, round path)``. Only an item the round checks is
    accepted: a blocker or should-fix finding of an earlier round that no
    reviewed round has checked closed and none has settled. A second check
    of an item replaces the first. Raises ``SpecfloError``, leaving the file
    untouched, on anything else.
    """
    if state not in CHECK_STATES:
        raise SpecfloError(
            f"Unknown state {state!r}. Valid values: " + ", ".join(CHECK_STATES) + "."
        )
    match = re.fullmatch(r"F-(\d+)", finding_id)
    if match is None:
        raise SpecfloError(f"{finding_id!r} is not a finding ID; one looks like F-01.")
    key = int(match.group(1))
    with locked(lock_path_for(root, slug, _LOCK_NAME)):
        path = open_round(root, cfg, slug)
        if path is None:
            raise SpecfloError(
                "No review is open. Start one with `specflo review start`."
            )
        doc = path.read_text()
        number = _round_number(path, frontmatter(path).get("round"))
        items, known, closed, settled = _ledger(root, cfg, slug, number)
        if key not in items:
            if key in _defined_numbers(doc):
                why = f"{finding_id} is a finding of this round; a round checks only earlier items."
            elif key not in known:
                why = f"No earlier round records a finding {finding_id}."
            elif known[key][1] == "nit":
                why = f"{known[key][0]} is a nit, and a nit is never checked."
            elif key in settled:
                why = _settled_why(known[key][0], settled[key])
            else:
                why = f"{known[key][0]} was already checked closed in {closed[key]}."
            listed = ", ".join(items.values()) or "none"
            raise SpecfloError(f"{why} Items this round checks: {listed}.")
        finding_id = items[key]
        line = f"- {finding_id} {state}"
        body = markdown.section_body(doc, EARLIER_HEADER)
        if body is None:
            if markdown.section_body(doc, FINDINGS_HEADER) is not None:
                doc = markdown.ensure_section_before(doc, EARLIER_HEADER, FINDINGS_HEADER)
            else:
                doc = doc.rstrip("\n") + f"\n\n{EARLIER_HEADER}\n"
            body = ""
        lines = body.strip("\n").splitlines() if body.strip() else []
        for index, existing in enumerate(lines):
            found = _CHECK_LINE.match(existing.strip())
            if found and int(found.group(2)) == key:
                lines[index] = line
                break
        else:
            lines.append(line)
        path.write_text(markdown.replace_section_body(doc, EARLIER_HEADER, "\n".join(lines)))
    return finding_id, path


def check_fixes(root: Path, cfg: SpecfloConfig, slug: str, fixes: list[str]) -> list[str]:
    """The F-NN a fix task names, as the rounds spell them, or a refusal.

    A task fixes only an open item: a blocker or should-fix finding of a
    closed round that no reviewed round has checked closed and none has
    settled, which is what the next round checks. A value may list several
    IDs split by commas, as the task's Fixes field does. Raises
    ``SpecfloError`` naming the ID on anything that is not an F-NN, an ID no
    closed round records, a nit, an item checked closed, a settled finding
    (naming how and in which round), and a finding of the open round, whose
    verdict is not decided yet. Reads the round files and changes nothing.
    """
    rounds = round_files(root, cfg, slug)
    current = open_round(root, cfg, slug)
    latest = rounds[-1][0] if rounds else 0
    items, known, closed, settled = _ledger(
        root, cfg, slug, latest if current else latest + 1
    )
    pending = _defined_numbers(current.read_text()) if current else set()
    listed = ", ".join(items.values()) or "none"
    accepted = []
    for fix in (part.strip() for value in fixes for part in value.split(",")):
        if not fix:
            continue
        match = re.fullmatch(r"F-(\d+)", fix)
        if match is None:
            raise SpecfloError(
                f"{fix!r} is not a finding ID; one looks like F-01."
                f" Open items a task can fix: {listed}."
            )
        key = int(match.group(1))
        if key not in items:
            if key in pending:
                why = (
                    f"{fix} is a finding of the open round {current.name}; close"
                    " the round with `specflo review done` first."
                )
            elif key not in known:
                why = f"No closed review round records a finding {fix}."
            elif known[key][1] == "nit":
                why = (
                    f"{known[key][0]} is a nit, and a task fixes only a blocker"
                    " or should-fix item."
                )
            elif key in settled:
                why = _settled_why(known[key][0], settled[key])
            else:
                why = f"{known[key][0]} was already checked closed in {closed[key]}."
            raise SpecfloError(f"{why} Open items a task can fix: {listed}.")
        accepted.append(items[key])
    return accepted


def _settled_why(finding_id: str, settled: tuple[str, str]) -> str:
    """Why a settled finding is no item: how it was settled, and in which round file."""
    kind, where = settled
    return f"{finding_id} was {kind} in {where}."


def settle_finding(
    root: Path, cfg: SpecfloConfig, slug: str, finding_id: str, kind: str,
    line: Callable[[Finding, Path], str],
) -> tuple[str, Path]:
    """Write a line under Settled in the round that recorded ``finding_id``.

    Returns ``(F-NN, that round's path)``, the ID as the round spells it.
    Only an open item is settled: a blocker or should-fix finding of a
    closed round that no reviewed round has checked closed and none has
    settled. ``kind`` is how it is settled, named in the refusals. ``line``
    is called with the finding, as that round records it, and the round's
    path only once every refusal has passed, under the lock, and gives the
    whole line to write, so whatever it records beside the line is recorded
    only for a finding that settles. A refusal it raises writes no line.

    The round's first settled finding appends the Settled section to its
    file. Every byte before the section stays: the round is closed, and its
    frontmatter, findings and checks are never rewritten.

    Raises ``SpecfloError``, leaving every file untouched, naming the ID on
    anything that is not an F-NN, an ID no closed round records, a nit, an
    item checked closed, a finding already settled, a finding of the open
    round, and an item the open round has checked: that check would then
    name no item, and the round could not close. Each refusal lists the
    open items.
    """
    match = re.fullmatch(r"F-(\d+)", finding_id)
    with locked(lock_path_for(root, slug, _LOCK_NAME)):
        rounds = round_files(root, cfg, slug)
        current = open_round(root, cfg, slug)
        latest = rounds[-1][0] if rounds else 0
        upto = latest if current else latest + 1
        items, known, closed, settled = _ledger(root, cfg, slug, upto)
        doc = current.read_text() if current else ""
        key = int(match.group(1)) if match else None
        checked = {
            int(found.group(2))
            for text in _section_lines(doc, EARLIER_HEADER) or []
            if (found := _CHECK_LINE.match(text))
        }
        why = None
        if key is None:
            why = f"{finding_id!r} is not a finding ID; one looks like F-01."
        elif key not in items:
            if key in _defined_numbers(doc):
                why = (
                    f"{finding_id} is a finding of the open round {current.name}; close"
                    " the round with `specflo review done` first."
                )
            elif key not in known:
                why = f"No closed review round records a finding {finding_id}."
            elif known[key][1] == "nit":
                why = f"{known[key][0]} is a nit, and a nit is never {kind}."
            elif key in settled:
                why = f"{known[key][0]} was already {settled[key][0]} in {settled[key][1]}."
            else:
                why = f"{known[key][0]} was already checked closed in {closed[key]}."
        elif key in checked:
            why = (
                f"{items[key]} is checked in the open round {current.name}; close"
                " the round with `specflo review done` first."
            )
        if why is not None:
            listed = ", ".join(items.values()) or "none"
            raise SpecfloError(f"{why} Open items: {listed}.")
        finding_id = items[key]
        # The round whose finding the ledger reads: the last that records it.
        path, finding = next(
            (path, finding) for n, path in reversed(rounds) if n < upto
            for text in _findings_lines(path.read_text()) or []
            if (finding := parse_finding_line(text)) and finding.number == key
        )
        entry = line(finding, path)
        text = path.read_text()
        body = markdown.section_body(text, SETTLED_HEADER)
        if body is None:
            # A blank line before the heading, whatever the file ends with.
            trailing = len(text) - len(text.rstrip("\n"))
            gap = "\n" * max(0, 2 - trailing) if text.strip() else ""
            text = f"{text}{gap}{SETTLED_HEADER}\n\n{entry}\n"
        else:
            kept = body.strip("\n")
            text = markdown.replace_section_body(
                text, SETTLED_HEADER, f"{kept}\n{entry}" if kept else entry
            )
        path.write_text(text)
    return finding_id, path


def reject_finding(
    root: Path, cfg: SpecfloConfig, slug: str, finding_id: str, reason: str
) -> tuple[str, Path]:
    """Settle an open item as rejected: '- F-NN rejected: <reason>' under
    Settled in the round that recorded it (see :func:`settle_finding`).

    Returns ``(F-NN, that round's path)``. The reason says why the finding
    is not a problem, so no fix is owed for it. Raises ``SpecfloError``,
    leaving every file untouched, on an empty or multi-line reason, before
    any file is read, and on anything :func:`settle_finding` refuses.
    """
    if not (reason or "").strip():
        raise SpecfloError(
            "A rejection needs a non-empty --reason, so the round that recorded the"
            " finding says why it is not a problem."
        )
    require_one_line("A rejection's --reason", reason)
    reason = reason.strip()
    return settle_finding(
        root, cfg, slug, finding_id, REJECTED,
        lambda finding, _: f"- {finding.id} {REJECTED}: {reason}",
    )


def defer_finding(
    root: Path, cfg: SpecfloConfig, slug: str, finding_id: str, do: str,
    hosted: bool = False,
) -> tuple[str, Path, str]:
    """Settle an open item as deferred: file a follow-up for it and write
    '- F-NN deferred FU-NN' under Settled in the round that recorded it (see
    :func:`settle_finding`).

    Returns ``(F-NN, that round's path, FU-NN)``. The follow-up's title is
    the finding's text, its Do line ``do``, what a later project should do,
    and its From line the round file and the F-NN, such as 'review-1.md
    F-01'. It is filed only once every refusal has passed, before the round
    is written, so a follow-up that cannot be added refuses the deferral and
    neither is written.

    ``hosted`` says a daemon holds the project. Follow-ups work only for
    projects in a checkout, so a hosted deferral is refused, naming the
    follow-up that will route them, before anything else is checked.

    Raises ``SpecfloError``, leaving every file untouched, on that, on an
    empty or multi-line ``do``, before any file is read, and on anything
    :func:`settle_finding` refuses.
    """
    if hosted:
        raise SpecfloError(_HOSTED_DEFER)
    if not (do or "").strip():
        raise SpecfloError(
            "A deferral needs a non-empty --do, so the follow-up says what a later"
            " project should do."
        )
    require_one_line("A deferral's --do", do)
    filed: list[str] = []

    def line(finding: Finding, path: Path) -> str:
        entry = followup.add_followup(
            root, cfg, slug, finding.text, do, source=f"{path.name} {finding.id}"
        )
        filed.append(entry.id)
        return f"- {finding.id} {DEFERRED} {entry.id}"

    deferred, path = settle_finding(root, cfg, slug, finding_id, DEFERRED, line)
    return deferred, path, filed[0]


def waive_round(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    reason: str,
    today: str | None = None,
    sha: str | None = None,
) -> Path:
    """Close the open round waived with ``reason``, or mint one and close it so.

    Works at any time, and past the level's round budget: a waive is the
    user's choice not to review, so it never needs ``--over-budget``. An
    empty reason is refused before anything is written.
    """
    if not (reason or "").strip():
        raise SpecfloError(
            "A waive needs a non-empty --reason, so a project that skipped review"
            " records why."
        )
    if open_round(root, cfg, slug) is None:
        start_round(root, cfg, slug, today=today, over_budget=True, sha=sha)
    return close_round(root, cfg, slug, WAIVED, reason=reason, today=today, sha=sha).path


ALREADY_SETTLED_HEADER = "## Already settled"
# The round file a follow-up recorded from a round names first in its From
# line: 'review-1.md' as the brief asks, 'review-1.md F-01' for a deferral.
_ROUND_SOURCE = re.compile(r"(review-\d+\.md)\b")


def _settled_entries(root: Path, cfg: SpecfloConfig, slug: str, number: int) -> list[str]:
    """What the rounds before round ``number`` settled, one brief line each.

    Each nit as its round records it, and each deferred or rejected finding
    so, with its follow-up or its reason; then each follow-up of the project
    whose From line names one of those rounds, except the one a round files
    for its nits and the one a deferral files, which its finding names. An
    item checked closed and an open item are not settled, so neither is here.
    """
    findings: dict[int, Finding] = {}
    settled: dict[int, tuple[str, str]] = {}
    earlier: dict[str, int] = {}
    for n, path in round_files(root, cfg, slug):
        if n >= number:
            break
        earlier[path.name] = n
        doc = path.read_text()
        for line in _findings_lines(doc) or []:
            finding = parse_finding_line(line)
            if finding:
                findings[finding.number] = finding
        settled.update(parse_settled(doc))
    lines = []
    for key, finding in sorted(findings.items()):
        if key in settled:
            kind, detail = settled[key]
            how = f"deferred to {detail}" if kind == DEFERRED else f"{REJECTED}: {detail}"
            lines.append(f"{render_finding_line(finding)} - {how}")
        elif finding.severity == "nit":
            lines.append(render_finding_line(finding))
    deferrals = {detail for kind, detail in settled.values() if kind == DEFERRED}
    # Read where the project lives: a daemon's project has a followup
    # document only if one was written there.
    for entry in followup.list_followups(root, cfg, include_closed=True):
        match = _ROUND_SOURCE.match(entry.source or "")
        if entry.project != slug or entry.id in deferrals or match is None:
            continue
        name = match.group(1)
        if name not in earlier or (
            entry.source == name and entry.title == _nits_title(earlier[name])
        ):
            continue
        lines.append(f"- {entry.id} {entry.title} - a follow-up recorded from {entry.source}")
    return lines


def settled_section(root: Path, cfg: SpecfloConfig, slug: str, number: int) -> list[str]:
    """The Already settled section of round ``number``'s brief, a blank line
    first, or no lines when the rounds before it settled nothing.

    It holds only settled items, so the reviewer reads new code with fresh
    eyes and does not anchor on open problems, and it says to raise one
    again only with new evidence that it is worse than recorded.
    """
    entries = _settled_entries(root, cfg, slug, number)
    if not entries:
        return []
    return [
        "",
        ALREADY_SETTLED_HEADER,
        "",
        "Earlier rounds settled these, so they are not findings again. Raise one"
        " again only with new evidence that it is worse than recorded, and say in"
        " the finding's text what that evidence is.",
        "",
        *entries,
    ]


def reviewer_brief(
    root: Path, cfg: SpecfloConfig, slug: str, hosted: bool = False,
    test_command: str | None = None,
) -> str:
    """The brief for the reviewer of the open round: one set of rules every round.

    Carries the round's scope (the whole branch, or the delta range and the
    earlier items to check), each item's fix tasks and the rules for checking
    an item closed: its pin test fails on the source at the latest reviewed
    round's sha and passes on HEAD, and the defect is gone on every path that
    reaches it. A round with no items has none of this. Then what earlier
    rounds settled, when they settled anything (see :func:`settled_section`),
    what each severity means, what is not a finding,
    how to record, that the CLI sets the verdict, and which tests to run:
    ``test_command`` each round when one is given, else only the tests in
    scope. ``test_command`` is the caller's checkout command: a daemon holds
    only the documents. ``hosted`` says a daemon holds the project, where
    follow-ups are not recorded, so the brief asks for such problems in the
    reply. Raises ``SpecfloError`` when no round is open.
    """
    scope = review_scope(root, cfg, slug)
    name = scope["file"]
    if scope["range"]:
        where = (
            f"This is a delta round: review only the changes in `{scope['range']}`"
            f" (`git diff {scope['range']}`), the fixes made since the last reviewed"
            " round."
        )
        outside = (
            "A problem the branch did not introduce, or one outside"
            f" `{scope['range']}`, is not a finding."
        )
    else:
        where = "This round reviews the whole branch: every change the branch makes."
        outside = "A problem the branch did not introduce is not a finding."
    lines = [
        f"# Reviewer brief: {slug}, review round {scope['round']} ({name})",
        "",
        "Review the work and record what you find through the specflo CLI.",
        "",
        "## Scope",
        "",
        where,
    ]
    if scope["items"]:
        fixes = _fix_tasks(root, cfg, slug, scope["items"])
        lines += [
            "",
            "Earlier rounds left these blocker and should-fix items. Check each one"
            " and record whether it is fixed. Under each item are the tasks that fix it:",
            "",
        ]
        for item in scope["items"]:
            lines.append(f"- {item}")
            lines += [
                f"  - {task.id} {task.text.rstrip('.')}. Verify: `{task.verify}`"
                for task in fixes[item]
            ] or ["  - No task fixes it."]
        # The fixes were made after the latest reviewed round, so its sha is
        # the source a pin test must fail on, in a full round too.
        numbered = [(n, p) for n, p in round_files(root, cfg, slug) if n < scope["round"]]
        sha = _reviewed_sha(numbered)
        before = f"at `{sha}`, the latest reviewed round's sha," if sha else "before its fix"
        lines += [
            "",
            "## Checking an item closed",
            "",
            "Check an item closed only after its pin test, the test its fix task's"
            f" verify step runs, fails on the source {before} and passes on HEAD. A"
            " pin test that passes before the fix proves nothing about the fix.",
            "",
            "Check an item closed only when the defect is gone on every path that"
            " reaches it, not only the path its finding names: search for the other"
            " code that reaches the same defect. A path the fix missed keeps the item"
            " open and is not a new finding: check the item open, and do not record"
            " the path with `specflo review finding add`.",
        ]
    lines += settled_section(root, cfg, slug, scope["round"])
    lines += [
        "",
        "## Severity",
        "",
        "- blocker: wrong behaviour, a broken requirement, or data loss.",
        "- should-fix: a real problem to fix before merge, smaller than a blocker.",
        "- nit: style, naming, wording or docs polish. Wording in agent-facing text"
        " (skills, prompts, messages an agent reads) is a nit unless it tells the"
        " agent to do the wrong thing.",
        "",
        "A blocker or should-fix finding asks for changes. A nit never blocks: it"
        + (" stays listed in the round." if hosted else " goes to a follow-up when the round closes."),
        "",
        "## What is not a finding",
        "",
        (
            f"{outside} Follow-ups work only for projects in a checkout, and a"
            " daemon holds this one: name such a problem in your reply instead,"
            " so it never blocks this round."
            if hosted else
            f"{outside} Record it with `specflo followup add \"<title>\" --do \"<what"
            f" to do>\" --from \"{name}\"` instead, so it never blocks this round."
        ),
        "",
        "## How to record",
        "",
        "- Each finding: `specflo review finding add --severity blocker|should-fix|nit"
        " --at <file>:<line>[-<line>] --text \"<one line>\"`. `--at` names where the"
        " defect is, as the file is at the round's sha; a blocker or should-fix needs"
        " it, and a nit may leave it out.",
    ]
    if scope["items"]:
        lines.append(
            "- Each earlier item above: `specflo review finding check F-NN closed|open`."
        )
    else:
        lines.append(
            "- No earlier items to check this round. A later round records each one"
            " with `specflo review finding check F-NN closed|open`."
        )
    lines += [
        f"- One line under `## Scope reviewed` in {name} saying what you read.",
        f"- A round with no findings: `- none` as the only line under `## Findings` in {name}.",
        "",
        "Do not choose a verdict. `specflo review done` derives it from what you"
        " recorded when the round closes.",
        "",
        "## Tests",
        "",
    ]
    # A configured test_command is the whole suite: the reviewer runs it every
    # round rather than guess which tests reach the changed code.
    lines.append(
        f"Run the whole test suite with `{test_command}` each round." if test_command else
        "Run only the tests for the files in scope. The whole suite ran before the"
        " first round."
    )
    return "\n".join(lines) + "\n"
