"""The ladder run: one auto run that climbs quick, fast and full.

A ladder is one project. It starts at quick level on its own branch, and when
a level completes the next auto pass cuts the next level's branch from there
and moves the project up, so the branches stack: quick's holds the smallest
finished change, full's the most complete one. ``ladder.md`` in the project
directory records the base the run started from and one row per level.

The ladder only makes local branches and commits. It never pushes, and never
deletes, renames or resets a branch.
"""

from __future__ import annotations

import datetime
import re
import subprocess
from pathlib import Path

from . import brainstorm, brief, markdown, plan, projects, review
from .config import CONFIG_DIRNAME, SpecfloConfig
from .errors import SpecfloError

LADDER_FILENAME = "ladder.md"
# How long one test_command run may take before it counts as failed.
TEST_TIMEOUT_SECONDS = 3600
BRANCH_PREFIX = "specflo"

_ROW_HEADER = (
    "| Level | Branch | Commits | Files | Added | Removed | Tasks | Tests"
    " | Review | Deferred | Time (s) |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|\n"
)


def branch_name(slug: str, level: str) -> str:
    return f"{BRANCH_PREFIX}/{slug}/{level}"


def ladder_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return projects.project_dir(root, cfg, slug) / LADDER_FILENAME


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SpecfloError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _check_start(root: Path, cfg: SpecfloConfig, project) -> None:
    """Refuse a ladder the run could not climb cleanly, naming the cause."""
    if project.level != projects.QUICK_LEVEL:
        raise SpecfloError(
            f"A ladder starts at quick level; project {project.slug!r} is at"
            f" {project.level!r}. Start one with `specflo new <name> --level quick`."
        )
    if project.status == projects.COMPLETE_STATUS:
        raise SpecfloError(f"Project {project.slug!r} is already complete.")
    try:
        _git(root, "rev-parse", "--is-inside-work-tree")
    except SpecfloError:
        raise SpecfloError(f"A ladder needs a git repository; {root} is not one.") from None
    # specflo's own documents change as the project is made and worked, so they
    # do not count; any other uncommitted change to a tracked file would ride
    # onto the ladder's first branch.
    excludes = [f":(exclude){CONFIG_DIRNAME}"]
    projects_root = (root / cfg.projects_dir).resolve()
    if projects_root.is_relative_to(root.resolve()):
        excludes.append(f":(exclude){projects_root.relative_to(root.resolve()).as_posix()}")
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no", "--", ":/", *excludes)
    if dirty:
        raise SpecfloError(
            "A ladder needs a clean tree: commit or stash these tracked changes"
            f" first:\n{dirty}"
        )
    for level in projects.LEVELS:
        name = branch_name(project.slug, level)
        if _git(root, "branch", "--list", name):
            raise SpecfloError(
                f"Branch {name!r} already exists; a ladder makes its own branches."
                " Delete or rename it yourself, or start the ladder under another name."
            )


def start(root: Path, cfg: SpecfloConfig, slug: str) -> dict:
    """Start a ladder on ``slug``: check, record the base, cut quick's branch.

    Returns the ladder record to keep in the auto run state.
    """
    project = projects.load_project(root, cfg, slug)
    _check_start(root, cfg, project)
    base_branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    base_commit = _git(root, "rev-parse", "HEAD")
    quick_branch = branch_name(slug, projects.QUICK_LEVEL)
    _git(root, "checkout", "-q", "-b", quick_branch)
    ladder_path(root, cfg, slug).write_text(
        f"# Ladder: {project.name}\n\n"
        f"Base: `{base_branch}` at `{base_commit}`\n\n" + _ROW_HEADER
    )
    return {
        "base_branch": base_branch,
        "base_commit": base_commit,
        "levels": {
            projects.QUICK_LEVEL: {
                "branch": quick_branch, "start_commit": base_commit, "started": _now(),
            },
        },
    }


def next_level(level: str) -> str | None:
    """The level a ladder climbs to after ``level``; None after full."""
    index = projects.LEVELS.index(level)
    return projects.LEVELS[index + 1] if index + 1 < len(projects.LEVELS) else None


def _diff_numbers(root: Path, start: str, end: str) -> tuple[int, int, int, int]:
    """Commits, files changed, lines added and lines removed from start to end."""
    commits = int(_git(root, "rev-list", "--count", f"{start}..{end}"))
    files = added = removed = 0
    for line in _git(root, "diff", "--numstat", start, end).splitlines():
        plus, minus, _ = line.split("\t", 2)
        files += 1
        # A binary file shows '-' for both counts: it changed, but has no lines.
        added += int(plus) if plus.isdigit() else 0
        removed += int(minus) if minus.isdigit() else 0
    return commits, files, added, removed


# A list item that says the list is empty, such as "- none", is not an item.
_EMPTY_ITEM_RE = re.compile(r"^(?:[-*]|\d+[.)])\s+(?:none|nothing|n/a)\W*$", re.IGNORECASE)


def _list_count(doc: str, header: str) -> int:
    body = markdown.strip_comments(markdown.section_body(doc, header) or "")
    return sum(
        1 for line in body.splitlines()
        if re.match(r"^(?:[-*]|\d+[.)])\s+\S", line) and not _EMPTY_ITEM_RE.match(line)
    )


def _deferred_count(root: Path, cfg: SpecfloConfig, slug: str, level: str) -> int:
    """The deferred list's length: the brief's at quick, the brainstorm's above."""
    base = projects.project_dir(root, cfg, slug)
    if level == projects.QUICK_LEVEL:
        path, header = base / brief.BRIEF_FILENAME, "## Deferred"
    else:
        path, header = base / brainstorm.BRAINSTORM_FILENAME, "## Out of scope / Deferred"
    return _list_count(path.read_text(), header) if path.is_file() else 0


def _tasks_cell(root: Path, cfg: SpecfloConfig, slug: str, level: str) -> str:
    path = plan.plan_path(root, cfg, slug)
    if level == projects.QUICK_LEVEL or not path.is_file():
        return "n/a"
    progress = plan.progress_from_doc(path.read_text())
    return f"{progress['done']}/{progress['total']}"


def _test_result(root: Path, cfg: SpecfloConfig) -> str:
    """The test_command's result on the checked-out branch, or 'not run'."""
    command = getattr(cfg, "test_command", None)
    if not command:
        return "not run"
    # The command is the user's shell line, so the shell is the program. A
    # command that hangs must not hold the auto pass for ever: past the limit
    # it counts as a failure.
    try:
        result = subprocess.run(
            ["sh", "-c", command], cwd=root, capture_output=True, timeout=TEST_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        return "fail"
    return "pass" if result.returncode == 0 else "fail"


def _seconds(started: str, ended: str) -> int:
    delta = datetime.datetime.fromisoformat(ended) - datetime.datetime.fromisoformat(started)
    return max(0, int(delta.total_seconds()))


# The reason a ladder records when it waives a level's review at the round
# budget. A ladder has no user to ask, and a level that did not converge in
# its budget is itself a result the comparison shows.
BUDGET_WAIVE_REASON = "review budget reached in a ladder run"


def _open_items(root: Path, cfg: SpecfloConfig, slug: str, state: dict) -> list[str]:
    """The blocker and should-fix items the latest round leaves open."""
    return review.items_to_check(root, cfg, slug, state["latest"] + 1)


def _review_cell(root: Path, cfg: SpecfloConfig, slug: str, level: str) -> str:
    """A level's Review cell: its latest verdict, or the budget waive and its open items."""
    state = review.review_state(root, cfg, slug)
    if level == projects.QUICK_LEVEL or state is None:
        return "none"
    if state["verdict"] == review.WAIVED and state["reason"] == BUDGET_WAIVE_REASON:
        items = _open_items(root, cfg, slug, state)
        return "waived (budget)" + (f": {', '.join(items)}" if items else "")
    return state["verdict"] or "open"


def waive_for_budget(root: Path, cfg: SpecfloConfig, slug: str) -> str:
    """Waive the level's review at its round budget; the clause the pass adds.

    The open items stay open: the next level's first round is a delta round
    that must check them, and at full level the ladder's row lists them.
    """
    from .service.resolve import local_service

    level = projects.load_project(root, cfg, slug).level
    review.waive_round(root, cfg, slug, BUDGET_WAIVE_REASON)
    local_service(root, cfg).write_checkpoint(slug)
    items = _open_items(root, cfg, slug, review.review_state(root, cfg, slug))
    left = f" It leaves open: {', '.join(items)}." if items else ""
    return (
        f"- The {level} level used its review budget, so the ladder waived its review"
        f" ({BUDGET_WAIVE_REASON}).{left} Do not run another round: complete the level"
        " with `specflo advance`."
    )


def write_row(root: Path, cfg: SpecfloConfig, slug: str, record: dict, level: str) -> None:
    """Append ``level``'s row to ladder.md, once, from git and the documents."""
    entry = record["levels"][level]
    if entry.get("row_written"):
        return
    commits, files, added, removed = _diff_numbers(
        root, entry["start_commit"], entry["end_commit"]
    )
    cells = [
        level, f"`{entry['branch']}`", str(commits), str(files), str(added), str(removed),
        _tasks_cell(root, cfg, slug, level), _test_result(root, cfg),
        _review_cell(root, cfg, slug, level),
        str(_deferred_count(root, cfg, slug, level)),
        str(_seconds(entry["started"], entry["ended"])),
    ]
    with ladder_path(root, cfg, slug).open("a") as handle:
        handle.write("| " + " | ".join(cells) + " |\n")
    entry["row_written"] = True


def mark_end(root: Path, record: dict, level: str) -> None:
    """Record when ``level`` ended; a recorded end is kept."""
    record["levels"][level].setdefault("ended", _now())


def end_level(root: Path, cfg: SpecfloConfig, slug: str, record: dict, level: str) -> None:
    """Record where and when ``level`` ended, and write its row.

    The end commit is the branch tip now, not HEAD at the advance: a commit
    made after the advance is on the level's branch, so its row counts it.
    """
    mark_end(root, record, level)
    entry = record["levels"][level]
    if not entry.get("row_written"):
        entry["end_commit"] = _git(root, "rev-parse", "HEAD")
    write_row(root, cfg, slug, record, level)


def climb(root: Path, cfg: SpecfloConfig, slug: str, record: dict) -> str:
    """End the completed level, cut the next level's branch, and move up.

    The finished level's branch stays at its last commit. Returns the level
    the project is now at.
    """
    from .service.resolve import local_service

    level = projects.load_project(root, cfg, slug).level
    target = next_level(level)
    if target is None:
        raise SpecfloError("A ladder at full level has nowhere to climb.")
    if level not in record["levels"]:
        raise SpecfloError(
            f"The ladder has no record of the {level} level: the level was changed"
            " outside the ladder. Finish the project by hand, or start a new ladder."
        )
    if level == projects.QUICK_LEVEL and not brief.brief_path(root, cfg, slug).is_file():
        raise SpecfloError(
            f"Project {slug!r} has no brief.md to carry up, so the ladder cannot climb."
        )
    branch = branch_name(slug, target)
    if _git(root, "branch", "--list", branch):
        raise SpecfloError(
            f"Branch {branch!r} already exists, so the ladder cannot cut it. Rename or"
            " delete it yourself, then run `specflo auto` again."
        )
    # Cut the branch first: the row is written only once the climb can happen,
    # and the finished level's branch stays at the same commit either way.
    _git(root, "checkout", "-q", "-b", branch)
    end_level(root, cfg, slug, record, level)
    service = local_service(root, cfg)
    _, review_ids = service.set_level(slug, target)
    record["levels"][target] = {
        "branch": branch,
        "start_commit": record["levels"][level]["end_commit"],
        "started": _now(),
        "review": review_ids,
    }
    if target == projects.FULL_LEVEL:
        # Full level has its own review: an open round keeps the level from
        # completing on fast's verdict.
        review.start_round(root, cfg, slug)
    # The level and phase changed outside any command, so refresh what the
    # commands would have: the checkpoint, and the index where there is one.
    service.write_checkpoint(slug)
    if service.index_exists():
        service.write_index()
    return target


def over_cap_clause(outgrew: str, ladder: bool) -> str:
    """What an auto pass over its level's cap adds to the payload, in place of a stop."""
    tail = (
        " The ladder moves up when this level completes, and the next level picks"
        " up the deferred work."
        if ladder else
        " The run finishes at this level; the deferred work stays listed for the user."
    )
    return f"- {outgrew} Do not stop and do not move up a level yourself.{tail}"


def finish(root: Path, cfg: SpecfloConfig, slug: str, record: dict) -> str:
    """Write the full level's row and return the hand-off for the finished ladder."""
    from .config import display_path

    missing = [level for level in projects.LEVELS if level not in record["levels"]]
    if missing:
        raise SpecfloError(
            f"The ladder has no record of the {', '.join(missing)} level: the level was"
            " changed outside the ladder, so it has no branch of its own to compare."
        )
    end_level(root, cfg, slug, record, projects.FULL_LEVEL)
    branches = ", ".join(f"`{record['levels'][level]['branch']}`" for level in projects.LEVELS)
    where = display_path(ladder_path(root, cfg, slug), root)
    return (
        f"The ladder run for {slug!r} is complete. Its branches, smallest change"
        f" first: {branches}. The comparison is in {where}. Stop the auto run and"
        " hand off to the human: they review the branches and merge the one they"
        " like best. The ladder merges, pushes and deletes nothing."
    )
