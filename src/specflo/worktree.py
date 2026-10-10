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
import posixpath
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
    # The checkout root's place inside the worktree: ``path`` itself unless
    # the root is below the repository's top.
    workdir: Path | None = None


def git(cwd: Path, *args: str, check: bool = True, env: dict | None = None) -> str:
    """Run git in ``cwd`` and return its stdout, stripped; a refusal on failure."""
    result = _run(cwd, *args, env=env)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SpecfloError(f"git {args[0]} failed: {detail}")
    return result.stdout.strip()


def _run(
    cwd: Path, *args: str, env: dict | None = None, raw: bool = False,
) -> subprocess.CompletedProcess:
    """Run git in ``cwd``. Output is text, with bytes that are not UTF-8
    kept as surrogates; ``raw`` keeps stdout as the bytes git wrote."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, timeout=120, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SpecfloError(f"git {args[0]} could not run: {exc}") from exc
    if not raw:
        result.stdout = result.stdout.decode("utf-8", "surrogateescape")
    result.stderr = result.stderr.decode("utf-8", "replace")
    return result


def shown(name: str) -> str:
    """``name`` as text that is always valid UTF-8: the bytes of a file name
    that are not UTF-8 are written as ``\\xNN`` escapes."""
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def current_branch(root: Path) -> str | None:
    """The branch the checkout is on, or None for a detached HEAD.

    Read from the full symbolic ref, so a tag that shares the branch's name
    cannot turn it into an ambiguous short name.
    """
    result = _run(Path(root), "symbolic-ref", "--quiet", "HEAD")
    ref = result.stdout.strip()
    if result.returncode != 0 or not ref.startswith("refs/heads/"):
        return None
    return ref.removeprefix("refs/heads/")


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
    base_branch = current_branch(root)
    if base_branch is None:
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
    workdir = path / git(root, "rev-parse", "--show-prefix")
    workdir.mkdir(parents=True, exist_ok=True)
    missing = copy_included(root, workdir)
    return Created(path, branch, base_branch, base_sha, missing, workdir)


def included(root: Path) -> list[str]:
    """The paths ``.specflo/worktree-include`` lists, relative to the checkout
    root; blank lines and ``#`` lines skipped. Empty with no list."""
    include = Path(root) / CONFIG_DIRNAME / INCLUDE_FILENAME
    if not include.is_file():
        return []
    names = (raw.strip() for raw in include.read_text().splitlines())
    return [name for name in names if name and not name.startswith("#")]


def copy_included(root: Path, path: Path) -> list[str]:
    """Copy each path ``.specflo/worktree-include`` lists into the worktree at ``path``.

    The list names files git does not carry (a ``.env``, a local config), one
    per line, relative to the checkout root; a blank line or a ``#`` line is
    skipped. Returns the listed paths that were not copied: those that do
    not exist, and those that lead outside the checkout.
    """
    missing = []
    top = Path(root).resolve()
    for name in included(root):
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
    """Remove the worktree that holds ``path`` and delete ``branch``, whatever
    either holds.

    ``path`` may be the worktree's top or a directory inside it. A part that
    is gone already is skipped, so a half-made worktree is cleaned up too.
    """
    root = Path(root)
    if Path(path).exists():
        found = _run(Path(path), "rev-parse", "--show-toplevel")
        top = found.stdout.strip() if found.returncode == 0 else str(path)
        git(root, "worktree", "remove", "--force", top)
    git(root, "worktree", "prune")
    if _run(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        git(root, "branch", "-D", branch)


@dataclass
class Landed:
    """A landed task: its one commit on the base branch and the files it changed."""

    sha: str
    # Each changed file from the checkout root, as Files names them; a file
    # outside the root starts with ``../``.
    files: list[str]


PATCHES_DIRNAME = "patches"


def save_patch(root: Path, path: Path, base_sha: str, slug: str, task_id: str,
               stamp: str) -> Path | None:
    """Write what the worktree at ``path`` holds against ``base_sha`` to a patch file.

    The patch is under ``.specflo/worktrees/patches/<slug>/``, named for the
    task and ``stamp``, and applies with ``git apply`` on ``base_sha``.
    Returns its path, or None when the worktree holds no change.
    """
    tree = snapshot_tree(path, included(root))
    # Plumbing with fixed prefixes and raw bytes: no user diff config, colour
    # or newline translation reaches the patch.
    diff = _run(
        Path(root), "diff-tree", "-r", "-p", "--binary", "--full-index", "--no-color",
        "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", base_sha, tree, raw=True,
    )
    if diff.returncode != 0:
        raise SpecfloError(f"git diff-tree failed: {diff.stderr.strip()}")
    if not diff.stdout:
        return None
    directory = worktrees_dir(root) / PATCHES_DIRNAME / slug
    directory.mkdir(parents=True, exist_ok=True)
    patch = directory / f"{task_id}-{stamp}.patch"
    patch.write_bytes(diff.stdout)
    if not _applies(root, base_sha, patch):
        raise SpecfloError(
            f"The saved patch {relative(root, patch)} does not apply on {base_sha[:12]};"
            f" the worktree {relative(root, path)} is kept as it is."
        )
    return patch


def _applies(root: Path, base_sha: str, patch: Path) -> bool:
    """Whether ``patch`` applies with ``git apply`` on ``base_sha``, checked
    in a scratch index so no worktree or index changes."""
    index = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path",
                     "specflo-patch-index"))
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    # From a subdirectory git apply skips paths outside it, so it runs at the top.
    top = Path(git(root, "rev-parse", "--show-toplevel"))
    try:
        git(top, "read-tree", base_sha, env=env)
        return _run(top, "apply", "--cached", "--check", str(patch), env=env).returncode == 0
    finally:
        index.unlink(missing_ok=True)


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
        root, "merge-tree", "--write-tree", "-z", "--name-only", "--no-messages",
        "--merge-base", base_sha, tip, squashed,
    )
    lines = result.stdout.split("\0")
    if result.returncode == 1:
        conflicts = list(dict.fromkeys(shown(line) for line in lines[1:] if line))
        raise SpecfloError(
            f"The task's change conflicts with {base_branch} in {', '.join(conflicts)}."
            " Nothing was landed. Resolve it in the worktree against"
            f" {base_branch}, or reopen the task and dispatch it again."
        )
    if result.returncode != 0 or not lines[0]:
        detail = (result.stderr or result.stdout).strip()
        raise SpecfloError(f"git merge-tree failed: {detail}")
    return lines[0]


def changed_paths(root: Path, old: str, new: str) -> list[str]:
    """Every path, from the repository's top, that differs between ``old`` and ``new``.

    Renames are not detected, so a moved file counts at both its old and its
    new path, ``-z`` keeps each name as it is, unquoted, and ``--no-relative``
    keeps a ``diff.relative`` setting from cutting the paths to the root.
    """
    out = git(root, "diff", "-z", "--name-only", "--no-renames", "--no-relative", old, new)
    return [name for name in out.split("\0") if name]


def snapshot_tree(path: Path, exclude: list[str] | None = None) -> str:
    """The tree of everything the worktree at ``path`` holds, committed or not.

    Built in a scratch index, so the worktree's own index, branch and files
    are left as they are. Ignored files stay out, as they would from a commit,
    and so do the new files under ``exclude`` (paths from ``path``): the
    copies ``.specflo/worktree-include`` brought in.
    """
    index = Path(git(path, "rev-parse", "--path-format=absolute", "--git-path",
                     "specflo-land-index"))
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    try:
        git(path, "read-tree", "HEAD", env=env)
        git(path, "add", "-A", env=env)
        if exclude:
            added = git(path, "diff", "--cached", "-z", "--name-only", "--no-renames",
                        "--no-relative", "--diff-filter=A", "HEAD", "--",
                        *(f":(literal){name}" for name in exclude), env=env)
            names = [name for name in added.split("\0") if name]
            if names:
                git(path, "rm", "--cached", "-q", "--",
                    *(f":(top,literal){name}" for name in names), env=env)
        return git(path, "write-tree", env=env)
    finally:
        index.unlink(missing_ok=True)


def land(
    root: Path, path: Path, branch: str, base_branch: str, message: str,
    projects_dir: Path | None = None,
) -> Landed:
    """Land the task worktree at ``path`` on ``base_branch`` as one commit.

    What the worktree holds, its branch's commits and its uncommitted
    changes alike, becomes one commit on the merge base of the worktree's
    HEAD and the base branch's tip: the sha it was cut from, until it takes in
    the base branch, as it does to resolve a conflict. That commit is
    replayed onto the tip with ``git merge-tree``, which touches no
    worktree, and the checkout at ``root`` is fast-forwarded to it. The
    worktree and its branch are removed last.

    Refused, with nothing changed: when the checkout is not on
    ``base_branch``; when the worktree holds no change; when the change
    touches a path under ``projects_dir``, whose documents only specflo
    writes, in the checkout; when the replay conflicts; and when the
    checkout has uncommitted changes to a file the commit changes.
    Uncommitted changes to other files stay as they are.
    """
    root = Path(root)
    current = current_branch(root) or "no branch (detached HEAD)"
    if current != base_branch:
        raise SpecfloError(
            f"The checkout is on {current}, not {base_branch}, the branch the task"
            f" lands on. Check out {base_branch} first."
        )
    tree = snapshot_tree(path, included(root))
    tip = git(root, "rev-parse", f"refs/heads/{base_branch}")
    base_sha = git(root, "merge-base", git(path, "rev-parse", "HEAD"), tip)
    if tree == git(root, "rev-parse", f"{base_sha}^{{tree}}"):
        raise SpecfloError(f"{relative(root, path)} holds no change to land.")
    top = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
    touched = changed_paths(root, base_sha, tree)
    guarded = _under(top, projects_dir) if projects_dir is not None else None
    if guarded is not None:
        documents = [shown(f) for f in touched if f == guarded or f.startswith(guarded + "/")]
        if documents:
            raise SpecfloError(
                f"The task changes {', '.join(documents)}, under {guarded}: project"
                " documents change only through specflo. Undo that in the worktree,"
                " then land again."
            )
    if tip != base_sha:
        squashed = git(root, "commit-tree", tree, "-p", base_sha, "-m", message)
        tree = _replay(root, base_sha, tip, squashed, base_branch)
    sha = git(root, "commit-tree", tree, "-p", tip, "-m", message)
    files = changed_paths(root, tip, sha)
    dirty = sorted(shown(f) for f in set(files) & _uncommitted(root))
    if dirty:
        raise SpecfloError(
            f"The checkout has uncommitted changes to {', '.join(dirty)}, which the"
            " task also changes. Commit or move them aside, then land again."
        )
    git(root, "merge", "--ff-only", "-q", sha)
    remove(root, path, branch)
    prefix = git(root, "rev-parse", "--show-prefix").rstrip("/") or "."
    return Landed(sha, [shown(posixpath.relpath(f, prefix)) for f in files])
