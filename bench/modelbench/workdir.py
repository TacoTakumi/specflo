"""The sealed workdir a bench run starts in.

`make_workdir` copies the fixture to a fresh directory outside the repo, seeds a
specflo project there at the run's level with that level's request, and then
makes one git commit of the whole tree. The agent's history is that one commit,
and the tree holds the fixture plus the specflo project and nothing else: no
held-out test, reference solution, level request file or bench code.

Seeding uses specflo verbs only (`init`, `new --level --summary`, `section set`).
The request goes where the level's first working artifact reads it:

- quick: the brief's Goal section (a quick project works one brief.md);
- fast and full: the brainstorm's Current understanding section.

The request title is also the project summary. Request headings become bold
lines, because a `#` heading inside a section body would split the artifact's
own sections. The active-project pointer lives in `.specflo/config.yaml`, which
specflo's init does not gitignore, so it is in the commit and `specflo status`
works in the workdir.

The specflo executable is a parameter. By default it is the one next to the
running interpreter (under `uv run`, this repo's `.venv/bin/specflo`), else the
first `specflo` on PATH; `make_workdir` checks that its `--version` matches this
repo's `__version__`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "bench"
FIXTURE = BENCH / "fixture"
LEVELS_DIR = BENCH / "fixture-levels"
LEVELS = ("quick", "fast", "full")

# The specflo artifact and section that carry the level's request.
REQUEST_ARTIFACT = {"quick": "brief", "fast": "brainstorm", "full": "brainstorm"}
REQUEST_SECTION = {"quick": "Goal", "fast": "Current understanding", "full": "Current understanding"}

# Paths specflo writes in the workdir; every other file must be a fixture file.
SPECFLO_PREFIXES = (".specflo/", "docs/projects/")

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".venv", "*.egg-info")
_IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv"}

# Git runs with no user or system config, a fixed identity and no hooks.
_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
_GIT_CONFIG = (
    "-c", "user.name=bench", "-c", "user.email=bench@localhost",
    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
    "-c", "init.defaultBranch=main",
)


class WorkdirError(RuntimeError):
    """A workdir that cannot be made or is not sealed."""


@dataclass(frozen=True)
class SealedWorkdir:
    path: Path
    level: str
    slug: str
    commit: str


def find_specflo() -> str:
    """The specflo next to this interpreter (the repo's venv under uv run), else the one on PATH."""
    sibling = Path(sys.executable).parent / "specflo"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    found = shutil.which("specflo")
    if found is None:
        raise WorkdirError("specflo not found next to the interpreter or on PATH")
    return found


def repo_version() -> str:
    """This repo's specflo version, read from src/specflo/__init__.py."""
    text = (REPO / "src" / "specflo" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if match is None:
        raise WorkdirError("no __version__ in src/specflo/__init__.py")
    return match.group(1)


def tree_files(root: Path) -> set[str]:
    """Every file under `root` as a posix relative path, without .git or caches."""
    files = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in _IGNORED_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if path.suffix == ".pyc" or not path.is_file():
            continue
        files.add(rel.as_posix())
    return files


def foreign_files(workdir: Path, fixture: Path = FIXTURE) -> list[str]:
    """Files in `workdir` that are neither an unchanged fixture file nor specflo's; sorted."""
    known = tree_files(fixture)
    foreign = []
    for rel in sorted(tree_files(workdir)):
        if rel.startswith(SPECFLO_PREFIXES):
            continue
        if rel not in known or (workdir / rel).read_bytes() != (fixture / rel).read_bytes():
            foreign.append(rel)
    return foreign


def _check_destination(dest: Path) -> Path:
    resolved = dest.resolve()
    if resolved == REPO or REPO in resolved.parents:
        raise WorkdirError(f"workdir must be outside the repo ({REPO}): {resolved}")
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise WorkdirError(f"workdir exists and is not empty: {resolved}")
    return resolved


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None, stdin: str | None = None) -> str:
    out = subprocess.run(
        cmd, cwd=cwd, env={**os.environ, **(env or {})}, input=stdin,
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise WorkdirError(f"{' '.join(cmd[:4])} failed ({out.returncode}): {out.stderr.strip()}")
    return out.stdout


def request_body(request: str) -> str:
    """The request with its heading lines turned into bold lines, so it fits one section."""
    lines = []
    for line in request.strip().splitlines():
        match = re.match(r"^#+\s+(.*)$", line)
        lines.append(f"**{match.group(1).strip()}**" if match else line)
    return "\n".join(lines) + "\n"


def request_title(request: str) -> str:
    """The request's first heading, or its first non-empty line."""
    for line in request.splitlines():
        if line.strip():
            return line.lstrip("#").strip()
    raise WorkdirError("level request is empty")


def make_workdir(
    dest: Path,
    level: str,
    *,
    fixture: Path = FIXTURE,
    levels_dir: Path = LEVELS_DIR,
    specflo: str | None = None,
) -> SealedWorkdir:
    """Copy the fixture to `dest`, seed a specflo project at `level`, commit once, check it is sealed."""
    if level not in LEVELS:
        raise WorkdirError(f"unknown level {level!r}: expected one of {', '.join(LEVELS)}")
    dest = _check_destination(dest)
    request_file = levels_dir / f"{level}.md"
    if not request_file.is_file():
        raise WorkdirError(f"no request for level {level!r}: {request_file}")
    request = request_file.read_text(encoding="utf-8")
    specflo = specflo or find_specflo()
    version = _run([specflo, "--version"]).strip()
    if repo_version() not in version.split():
        raise WorkdirError(f"{specflo} reports {version!r}, not this repo's {repo_version()}")

    if dest.exists():
        dest.rmdir()
    shutil.copytree(fixture, dest, ignore=_IGNORE)

    sf = [specflo, "-C", str(dest)]
    name = f"tinytodo {level}"
    _run([*sf, "init"], cwd=dest)
    _run([*sf, "new", name, "--level", level, "--summary", request_title(request)], cwd=dest)
    _run(
        [*sf, "section", "set", REQUEST_ARTIFACT[level], REQUEST_SECTION[level], "--stdin"],
        cwd=dest, stdin=request_body(request),
    )
    slug = json.loads(_run([*sf, "status", "--json"], cwd=dest)).get("active_project")
    if not slug:
        raise WorkdirError("specflo status shows no active project after seeding")

    git = ["git", "-C", str(dest), *_GIT_CONFIG]
    _run([*git, "init", "-q"], cwd=dest, env=_GIT_ENV)
    _run([*git, "add", "-A"], cwd=dest, env=_GIT_ENV)
    _run([*git, "commit", "-q", "-m", f"bench: tinytodo fixture, {level} level"], cwd=dest, env=_GIT_ENV)
    commit = _run([*git, "rev-parse", "HEAD"], cwd=dest, env=_GIT_ENV).strip()

    if _run([*git, "rev-list", "--count", "HEAD"], cwd=dest, env=_GIT_ENV).strip() != "1":
        raise WorkdirError("workdir history is not exactly one commit")
    if _run([*git, "status", "--porcelain"], cwd=dest, env=_GIT_ENV):
        raise WorkdirError("workdir tree is not clean after the commit")
    foreign = foreign_files(dest, fixture)
    if foreign:
        raise WorkdirError(f"workdir holds files that are not fixture or specflo files: {foreign}")
    return SealedWorkdir(path=dest, level=level, slug=slug, commit=commit)
