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
    dirty = _git(
        root, "status", "--porcelain", "--untracked-files=no", "--", ".",
        f":(exclude){cfg.projects_dir}", f":(exclude){CONFIG_DIRNAME}",
    )
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


def _list_count(doc: str, header: str) -> int:
    body = markdown.strip_comments(markdown.section_body(doc, header) or "")
    return sum(1 for line in body.splitlines() if re.match(r"^(?:[-*]|\d+[.)])\s+\S", line))


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
    result = subprocess.run(command, shell=True, cwd=root, capture_output=True)
    return "pass" if result.returncode == 0 else "fail"


def _seconds(started: str, ended: str) -> int:
    delta = datetime.datetime.fromisoformat(ended) - datetime.datetime.fromisoformat(started)
    return max(0, int(delta.total_seconds()))


def write_row(root: Path, cfg: SpecfloConfig, slug: str, record: dict, level: str) -> None:
    """Append ``level``'s row to ladder.md, once, from git and the documents."""
    entry = record["levels"][level]
    if entry.get("row_written"):
        return
    commits, files, added, removed = _diff_numbers(
        root, entry["start_commit"], entry["end_commit"]
    )
    state = review.review_state(root, cfg, slug)
    verdict = "none" if level == projects.QUICK_LEVEL or state is None else (state["verdict"] or "open")
    cells = [
        level, f"`{entry['branch']}`", str(commits), str(files), str(added), str(removed),
        _tasks_cell(root, cfg, slug, level), _test_result(root, cfg), verdict,
        str(_deferred_count(root, cfg, slug, level)),
        str(_seconds(entry["started"], entry["ended"])),
    ]
    with ladder_path(root, cfg, slug).open("a") as handle:
        handle.write("| " + " | ".join(cells) + " |\n")
    entry["row_written"] = True


def end_level(root: Path, cfg: SpecfloConfig, slug: str, record: dict, level: str) -> None:
    """Record where and when ``level`` ended, and write its row."""
    entry = record["levels"][level]
    entry.setdefault("end_commit", _git(root, "rev-parse", "HEAD"))
    entry.setdefault("ended", _now())
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
    end_level(root, cfg, slug, record, level)
    branch = branch_name(slug, target)
    _git(root, "checkout", "-q", "-b", branch)
    local_service(root, cfg).set_level(slug, target)
    record["levels"][target] = {
        "branch": branch,
        "start_commit": record["levels"][level]["end_commit"],
        "started": _now(),
    }
    return target


def cut_down_clause(level: str, outgrew: str) -> str:
    """The instruction a ladder level over its cap gets in place of a stop."""
    if level == projects.QUICK_LEVEL:
        how = (
            "keep one check in Done when and move the rest to the brief's Deferred"
            " section"
        )
    else:
        how = (
            f"keep at most {projects.FAST_MAX_TASKS} tasks and"
            f" {projects.FAST_MAX_DECISIONS} decisions; supersede the rest and list"
            " them in the brainstorm's Out of scope / Deferred section"
        )
    return (
        f"- Ladder run: this level is over its cap ({outgrew}). Do not stop and do"
        f" not move up yourself: {how}. The ladder moves up when this level"
        " completes, and the next level picks up the deferred work."
    )


def finish(root: Path, cfg: SpecfloConfig, slug: str, record: dict) -> str:
    """Write the full level's row and return the hand-off for the finished ladder."""
    from .config import display_path

    end_level(root, cfg, slug, record, projects.FULL_LEVEL)
    branches = ", ".join(f"`{record['levels'][level]['branch']}`" for level in projects.LEVELS)
    where = display_path(ladder_path(root, cfg, slug), root)
    return (
        f"The ladder run for {slug!r} is complete. Its branches, smallest change"
        f" first: {branches}. The comparison is in {where}. Stop the auto run and"
        " hand off to the human: they review the branches and merge the one they"
        " like best. The ladder merges, pushes and deletes nothing."
    )
