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

import contextlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONFIG_DIRNAME
from .errors import SpecfloError
from .locking import lock_path_for, locked

WORKTREES_DIRNAME = "worktrees"
INCLUDE_FILENAME = "worktree-include"
BRANCH_PREFIX = "specflo"
_GITIGNORE_CONTENT = "*\n"
# A start may wait behind several others, each making a whole worktree.
LOCK_TIMEOUT = 300.0


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


@contextlib.contextmanager
def creation_lock(root: Path, timeout: float = LOCK_TIMEOUT):
    """Hold the checkout's one worktree lock.

    ``git worktree add`` writes shared state in the repository, and a start
    reads the plan before it writes it, so starts that make a worktree run
    one at a time.
    """
    # No project slug starts with a dot, so the lock is no project's.
    with locked(lock_path_for(root, ".worktrees", "create"), timeout):
        yield


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


@dataclass
class Landed:
    """A landed task: its one commit on the base branch and the files it changed."""

    sha: str
    files: list[str]


def _under(top: Path, directory: Path) -> str | None:
    """``directory`` relative to the repository's top, or None outside it."""
    resolved = Path(directory).resolve()
    if not resolved.is_relative_to(top):
        return None
    return resolved.relative_to(top).as_posix()


def _uncommitted(root: Path) -> set[str]:
    """Every path, from the repository's top, that the checkout has changed,
    staged or not, and every untracked one."""
    out = _run(root, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    entries = out.split("\0")
    paths: set[str] = set()
    i = 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) > 3:
            paths.add(entry[3:])
            # A rename or copy carries its source path as the next entry.
            if entry[0] in "RC":
                i += 1
                if i < len(entries) and entries[i]:
                    paths.add(entries[i])
        i += 1
    return paths


def _replay(root: Path, base_sha: str, tip: str, squashed: str, base_branch: str) -> str:
    """The tree of ``squashed`` replayed from ``base_sha`` onto ``tip``.

    ``git merge-tree`` works on objects alone, so a conflict leaves no
    rebase in progress and every worktree as it was; it is refused naming
    the conflicting files.
    """
    result = _run(
        root, "merge-tree", "--write-tree", "--name-only", "--no-messages",
        "--merge-base", base_sha, tip, squashed,
    )
    lines = result.stdout.splitlines()
    if result.returncode == 1:
        conflicts = list(dict.fromkeys(line for line in lines[1:] if line))
        raise SpecfloError(
            f"The task's change conflicts with {base_branch} in {', '.join(conflicts)}."
            " Nothing was landed. Resolve it in the worktree against"
            f" {base_branch}, or reopen the task and dispatch it again."
        )
    if result.returncode != 0 or not lines:
        detail = (result.stderr or result.stdout).strip()
        raise SpecfloError(f"git merge-tree failed: {detail}")
    return lines[0]


def snapshot_tree(path: Path) -> str:
    """The tree of everything the worktree at ``path`` holds, committed or not.

    Built in a scratch index, so the worktree's own index, branch and files
    are left as they are. Ignored files stay out, as they would from a commit.
    """
    index = Path(git(path, "rev-parse", "--path-format=absolute", "--git-path",
                     "specflo-land-index"))
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    try:
        git(path, "read-tree", "HEAD", env=env)
        git(path, "add", "-A", env=env)
        return git(path, "write-tree", env=env)
    finally:
        index.unlink(missing_ok=True)


def land(
    root: Path, path: Path, branch: str, base_branch: str, base_sha: str, message: str,
    projects_dir: Path | None = None,
) -> Landed:
    """Land the task worktree at ``path`` on ``base_branch`` as one commit.

    What the worktree holds, its branch's commits and its uncommitted
    changes alike, becomes one commit on ``base_sha``. That commit is
    replayed onto the base branch's tip with ``git merge-tree``, which
    touches no worktree, and the checkout at ``root`` is fast-forwarded to
    it. The worktree and its branch are removed last.

    Refused, with nothing changed: when the checkout is not on
    ``base_branch``; when the worktree holds no change; when the change
    touches a path under ``projects_dir``, whose documents only specflo
    writes, in the checkout; when the replay conflicts; and when the
    checkout has uncommitted changes to a file the commit changes.
    Uncommitted changes to other files stay as they are.
    """
    root = Path(root)
    current = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if current != base_branch:
        raise SpecfloError(
            f"The checkout is on {current}, not {base_branch}, the branch the task"
            f" lands on. Check out {base_branch} first."
        )
    tree = snapshot_tree(path)
    if tree == git(root, "rev-parse", f"{base_sha}^{{tree}}"):
        raise SpecfloError(f"{relative(root, path)} holds no change to land.")
    top = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
    touched = git(root, "diff", "--name-only", base_sha, tree).splitlines()
    guarded = _under(top, projects_dir) if projects_dir is not None else None
    if guarded is not None:
        documents = [f for f in touched if f == guarded or f.startswith(guarded + "/")]
        if documents:
            raise SpecfloError(
                f"The task changes {', '.join(documents)}, under {guarded}: project"
                " documents change only through specflo. Undo that in the worktree,"
                " then land again."
            )
    tip = git(root, "rev-parse", f"refs/heads/{base_branch}")
    if tip != base_sha:
        squashed = git(root, "commit-tree", tree, "-p", base_sha, "-m", message)
        tree = _replay(root, base_sha, tip, squashed, base_branch)
    sha = git(root, "commit-tree", tree, "-p", tip, "-m", message)
    files = git(root, "diff", "--name-only", tip, sha).splitlines()
    dirty = sorted(set(files) & _uncommitted(root))
    if dirty:
        raise SpecfloError(
            f"The checkout has uncommitted changes to {', '.join(dirty)}, which the"
            " task also changes. Commit or move them aside, then land again."
        )
    git(root, "merge", "--ff-only", "-q", sha)
    remove(root, path, branch)
    return Landed(sha, files)
