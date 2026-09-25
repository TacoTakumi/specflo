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
import subprocess
from pathlib import Path

from . import projects
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
