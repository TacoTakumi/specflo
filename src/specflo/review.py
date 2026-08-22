"""Review rounds: the ``review-N.md`` artifacts a project accrues.

A round is one end-of-execute whole-branch review, minted from a skeleton by
``specflo review start`` and closed by ``specflo review done``. Every piece of
review state - the round number, whether a round is open, its verdict, date and
sha - lives in these files and nowhere else (D-03), so the read path is a glob
over the project directory plus a frontmatter parse.

Numbers are allocated one above the highest file present, so a deleted round
leaves a permanent gap rather than a reused identity (D-07).
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import yaml

from .config import SpecfloConfig
from .locking import lock_path_for, locked
from .projects import project_dir

_ROUND_RE = re.compile(r"^review-(\d+)\.md$")
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
sha: ''
reason: ''
---

# Review round {number}

## Scope reviewed

## Findings

## Verdict
"""


def round_path(root: Path, cfg: SpecfloConfig, slug: str, number: int) -> Path:
    return project_dir(root, cfg, slug) / f"review-{number}.md"


def round_numbers(root: Path, cfg: SpecfloConfig, slug: str) -> list[int]:
    """Every round number present in the project directory, ascending."""
    directory = project_dir(root, cfg, slug)
    if not directory.is_dir():
        return []
    numbers = [
        int(match.group(1))
        for entry in directory.iterdir()
        if (match := _ROUND_RE.match(entry.name)) and entry.is_file()
    ]
    return sorted(numbers)


def frontmatter(path: Path) -> dict:
    """A round file's frontmatter mapping; ``{}`` when it has none to read.

    A hand-mangled round file must not take the CLI down: it reads as a round
    with no verdict, which is the same as an open one, and the session is
    steered back into it rather than past it.
    """
    parts = path.read_text().split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}


def open_round(root: Path, cfg: SpecfloConfig, slug: str) -> Path | None:
    """The unclosed round's path, or None when every round carries a verdict.

    A round is open exactly while its frontmatter verdict is empty (REQ-03).
    At most one can be open, because minting refuses while one is; if a
    hand-edited directory holds several, the highest-numbered one wins - it is
    the latest round, and the latest is what every surface reports (REQ-19).
    """
    for number in reversed(round_numbers(root, cfg, slug)):
        path = round_path(root, cfg, slug, number)
        if not str(frontmatter(path).get("verdict", "") or ""):
            return path
    return None


def start_round(
    root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
) -> tuple[Path, bool]:
    """Mint the next round file, or hand back the open one (REQ-01..REQ-03).

    Returns ``(path, created)``; ``created`` is False when a round was already
    open, which is never an error - reusing it is how an abandoned review is
    resumed, and there is deliberately no way to discard one.
    """
    today = today or datetime.date.today().isoformat()
    directory = project_dir(root, cfg, slug)
    with locked(lock_path_for(root, slug, _LOCK_NAME)):
        existing = open_round(root, cfg, slug)
        if existing is not None:
            return existing, False
        number = (max(round_numbers(root, cfg, slug), default=0)) + 1
        path = directory / f"review-{number}.md"
        path.write_text(_TEMPLATE.format(number=number, today=today))
    return path, True
