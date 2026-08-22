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


def start_round(
    root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
) -> Path:
    """Mint the next round file and return its path (REQ-01, REQ-02)."""
    today = today or datetime.date.today().isoformat()
    directory = project_dir(root, cfg, slug)
    with locked(lock_path_for(root, slug, _LOCK_NAME)):
        number = (max(round_numbers(root, cfg, slug), default=0)) + 1
        path = directory / f"review-{number}.md"
        path.write_text(_TEMPLATE.format(number=number, today=today))
    return path
