"""Task worktrees: a git worktree per delegated task, landed back one at a time.

``task start --worktree`` gives a task its own worktree under
``.specflo/worktrees/<project>/<task>`` on a new branch
``specflo/<project>/<task>`` cut from the checkout's HEAD. The subagent edits
and tests only there. ``task land`` turns what the worktree holds into one
commit on the base branch, and ``task reopen`` saves it as a patch. Every
git command here runs in the caller's checkout: a daemon holds no code, so a
hosted project gets no worktree.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONFIG_DIRNAME
from .errors import SpecfloError

WORKTREES_DIRNAME = "worktrees"
INCLUDE_FILENAME = "worktree-include"
BRANCH_PREFIX = "specflo"
_GITIGNORE_CONTENT = "*\n"


@dataclass
class Created:
    """A new task worktree: where it is, its branch, and what it was cut from."""

    path: Path
    branch: str
    base_branch: str
    base_sha: str
    missing: list[str] = field(default_factory=list)


def git(cwd: Path, *args: str, check: bool = True, env: dict | None = None) -> str:
    """Run git in ``cwd`` and return its stdout, stripped; a refusal on failure."""
    result = _run(cwd, *args, env=env)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SpecfloError(f"git {args[0]} failed: {detail}")
    return result.stdout.strip()


def _run(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SpecfloError(f"git {args[0]} could not run: {exc}") from exc


def worktrees_dir(root: Path) -> Path:
    """``<root>/.specflo/worktrees``, made with a self-ignoring ``.gitignore``,
    so no worktree under it shows in the checkout's ``git status``."""
    directory = Path(root) / CONFIG_DIRNAME / WORKTREES_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE_CONTENT, encoding="utf-8")
    return directory


def task_path(root: Path, slug: str, task_id: str) -> Path:
    return Path(root) / CONFIG_DIRNAME / WORKTREES_DIRNAME / slug / task_id


def task_branch(slug: str, task_id: str) -> str:
    return f"{BRANCH_PREFIX}/{slug}/{task_id}"


def relative(root: Path, path: Path) -> str:
    """``path`` as the task records it: relative to the checkout root."""
    return Path(path).relative_to(root).as_posix()


def create(root: Path, slug: str, task_id: str) -> Created:
    """Make the task's worktree on a new branch from the checkout's HEAD.

    Refused, with nothing made, when ``root`` is not a git checkout, when
    HEAD is on no branch, or when the task's path or branch is taken.
    """
    root = Path(root)
    base_branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if base_branch == "HEAD":
        raise SpecfloError(
            "The checkout is on no branch (detached HEAD); check out the branch the"
            " task lands on first."
        )
    base_sha = git(root, "rev-parse", "HEAD")
    path = task_path(root, slug, task_id)
    branch = task_branch(slug, task_id)
    if path.exists():
        raise SpecfloError(f"{relative(root, path)} exists already; remove it first.")
    if _run(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        raise SpecfloError(f"Branch {branch} exists already; delete it first.")
    worktrees_dir(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-q", "-b", branch, str(path), base_sha)
    missing = copy_included(root, path)
    return Created(path, branch, base_branch, base_sha, missing)


def copy_included(root: Path, path: Path) -> list[str]:
    """Copy each path ``.specflo/worktree-include`` lists into the worktree at ``path``.

    The list names files git does not carry (a ``.env``, a local config), one
    per line, relative to the checkout root; a blank line or a ``#`` line is
    skipped. Returns the listed paths that were not copied: those that do
    not exist, and those that lead outside the checkout.
    """
    include = Path(root) / CONFIG_DIRNAME / INCLUDE_FILENAME
    if not include.is_file():
        return []
    missing = []
    top = Path(root).resolve()
    for raw in include.read_text().splitlines():
        name = raw.strip()
        if not name or name.startswith("#"):
            continue
        source = (top / name).resolve()
        if not source.is_relative_to(top) or not source.exists():
            missing.append(name)
            continue
        target = Path(path) / source.relative_to(top)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    return missing


def remove(root: Path, path: Path, branch: str) -> None:
    """Remove the worktree at ``path`` and delete ``branch``, whatever either holds.

    A part that is gone already is skipped, so a half-made worktree is
    cleaned up too.
    """
    root = Path(root)
    if Path(path).exists():
        git(root, "worktree", "remove", "--force", str(path))
    git(root, "worktree", "prune")
    if _run(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        git(root, "branch", "-D", branch)
