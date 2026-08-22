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
import subprocess
from pathlib import Path

import yaml

from .config import SpecfloConfig
from .errors import SpecfloError
from .locking import lock_path_for, locked
from .projects import project_dir

_ROUND_RE = re.compile(r"^review-(\d+)\.md$")
# The whole verdict vocabulary (D-06). ready-to-merge and waived pass the
# completion gate; changes-requested blocks it.
READY = "ready-to-merge"
CHANGES_REQUESTED = "changes-requested"
WAIVED = "waived"
VERDICTS = (READY, CHANGES_REQUESTED, WAIVED)
# Frontmatter key order, pinned so a close rewrites a round file in the
# shape `review start` minted it.
_FIELDS = ("round", "verdict", "date", "sha", "reason")
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


def body_of(path: Path) -> str:
    """A round file's body: everything after its frontmatter."""
    parts = path.read_text().split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        return path.read_text()
    return parts[2].lstrip("\n")


def _render(fields: dict, body: str) -> str:
    """A round file from its frontmatter mapping and body.

    Keys keep their minted order; any key a human added survives after them,
    since the CLI reads only the five it wrote.
    """
    ordered = {key: fields.get(key, "") or "" for key in _FIELDS}
    ordered["round"] = int(ordered["round"] or 0)
    ordered.update({k: v for k, v in fields.items() if k not in _FIELDS})
    frontmatter_text = yaml.safe_dump(ordered, sort_keys=False).strip()
    return f"---\n{frontmatter_text}\n---\n\n{body}"


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


def close_round(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    verdict: str,
    reason: str | None = None,
    today: str | None = None,
) -> Path:
    """Close the open round with ``verdict``, date and sha (REQ-04, REQ-05, REQ-07).

    The date and sha stamp the close, overwriting the mint-time date: what
    matters is when the review was decided, not when its file appeared.

    Raises ``SpecfloError`` - leaving every file untouched - when the verdict is
    not one of :data:`VERDICTS`, when ``waived`` comes without a reason
    (REQ-06), or when no round is open.
    """
    if verdict not in VERDICTS:
        raise SpecfloError(
            f"Unknown verdict {verdict!r}. Valid values: " + ", ".join(VERDICTS) + "."
        )
    if verdict == WAIVED and not (reason or "").strip():
        raise SpecfloError(
            "Verdict 'waived' needs a --reason, so a project that skipped review"
            " records why."
        )
    with locked(lock_path_for(root, slug, _LOCK_NAME)):
        path = open_round(root, cfg, slug)
        if path is None:
            raise SpecfloError(
                "No review is open. Start one with `specflo review start`."
            )
        fields = frontmatter(path)
        fields["verdict"] = verdict
        fields["date"] = today or datetime.date.today().isoformat()
        fields["sha"] = head_sha(root)
        if reason is not None:
            fields["reason"] = reason
        path.write_text(_render(fields, body_of(path)))
    return path
