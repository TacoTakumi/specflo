"""A brief inside a running project.

A project that lives past its plan grows by ad hoc work: the user reports a
problem or an idea, the agent does recon, the user picks, and tasks follow.
A brief is the one place for that output: the ask in the user's words, the
facts recon found, the decisions made, the contract that is true after, and
what the brief found and does not do. It lives in ``briefs/B-NN-<slug>.md``
under the project and the project keeps its level and phase. Tasks cite it
with ``task add --from B-NN``.

This is not the quick level's ``brief.md`` (``brief.py``): that one is a
whole project's single document; this one is a feature inside a project.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

from . import markdown
from .config import SpecfloConfig
from .errors import SpecfloError, require_one_line
from .locking import lock_path_for, locked
from .projects import load_project, project_dir, slugify

BRIEFS_DIRNAME = "briefs"
ID_PREFIX = "B-"

# The brief's sections, in document order. Decisions is managed: only
# `decision add --brief B-NN` writes there.
SECTIONS = ("Ask", "Facts", "Decisions", "Contract", "Deferred")
MANAGED_SECTION = "Decisions"

_ID_RE = re.compile(r"^B-\d+$")
_FILENAME_RE = re.compile(r"^(B-\d+)-[a-z0-9-]+\.md$")

_TEMPLATE = """\
---
brief: {brief_id}
project: {slug}
title: {title}
sha: {sha}
created: {today}
updated: {today}
---

# {brief_id} - {title}

## Ask
<!-- the user's words, and the date -->

## Facts
<!-- what recon found, with file references -->

## Decisions
<!-- append-only; managed by `specflo decision add --brief {brief_id}`. Stable IDs D-NN. -->

## Contract
<!-- what is true after this brief's tasks are done -->

## Deferred
<!-- work this brief found and does not do (optional) -->
"""


@dataclass(frozen=True)
class Brief:
    id: str
    title: str
    path: Path
    sha: str


def briefs_dir(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return project_dir(root, cfg, slug) / BRIEFS_DIRNAME


def brief_files(root: Path, cfg: SpecfloConfig, slug: str) -> list[tuple[str, Path]]:
    """Every brief of the project as ``(B-NN, path)``, in id order."""
    directory = briefs_dir(root, cfg, slug)
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        match = _FILENAME_RE.match(path.name)
        if match and path.is_file():
            found.append((match.group(1), path))
    return sorted(found, key=lambda item: int(item[0][len(ID_PREFIX):]))


def brief_path(root: Path, cfg: SpecfloConfig, slug: str, brief_id: str) -> Path:
    """The file of brief ``brief_id``; refused when the project has no such brief."""
    if not _ID_RE.match(brief_id or ""):
        raise SpecfloError(f"Not a brief id: {brief_id!r} (expected B-NN).")
    for found_id, path in brief_files(root, cfg, slug):
        if found_id == brief_id:
            return path
    raise SpecfloError(f"No brief {brief_id} in project {slug!r}.")


def read_brief(root: Path, cfg: SpecfloConfig, slug: str, brief_id: str) -> str:
    return brief_path(root, cfg, slug, brief_id).read_text()


def is_brief_id(ref: str) -> bool:
    """Whether ``ref`` names a brief (B-NN) rather than a requirement or finding."""
    return bool(_ID_RE.match(ref or ""))


def brief_body(doc: str) -> str:
    """The brief from its ``# B-NN - title`` heading on: everything but the frontmatter."""
    match = re.search(r"^# B-\d+ - .*$", doc, re.MULTILINE)
    return doc[match.start():].rstrip() if match else doc.rstrip()


def brief_title(doc: str) -> str:
    """The title from the brief's ``# B-NN - title`` heading; "" when absent."""
    match = re.search(r"^# B-\d+ - (.+)$", doc, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _next_id(root: Path, cfg: SpecfloConfig, slug: str) -> str:
    numbers = [int(found_id[len(ID_PREFIX):]) for found_id, _ in brief_files(root, cfg, slug)]
    return f"{ID_PREFIX}{(max(numbers) if numbers else 0) + 1:02d}"


def add_brief(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    title: str,
    sha: str = "",
    today: str | None = None,
) -> Brief:
    """Create brief ``B-NN`` for ``title`` in the project and return it.

    ``sha`` is the commit the brief starts at, read by the caller in the
    checkout that holds the code; "" when git cannot answer. The project's
    level and phase are untouched.
    """
    require_one_line("A brief's title", title)
    project = load_project(root, cfg, slug)
    today = today or datetime.date.today().isoformat()
    directory = briefs_dir(root, cfg, slug)
    with locked(lock_path_for(root, slug, directory / ".briefs")):
        directory.mkdir(parents=True, exist_ok=True)
        brief_id = _next_id(root, cfg, slug)
        path = directory / f"{brief_id}-{slugify(title)}.md"
        path.write_text(_TEMPLATE.format(
            brief_id=brief_id, slug=project.slug, title=title, sha=sha, today=today,
        ))
    return Brief(id=brief_id, title=title, path=path, sha=sha)


def _section_title(section: str) -> str | None:
    """The canonical section title ``section`` names, or None."""
    wanted = section.strip().lstrip("#").strip().casefold()
    for title in SECTIONS:
        if title.casefold() == wanted:
            return title
    return None


def set_section(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    brief_id: str,
    section: str,
    body: str,
    today: str | None = None,
) -> str:
    """Replace the body of one prose section of brief ``brief_id``.

    Returns the section title written. Every other section stays as it is and
    ``updated`` is bumped. Refuses the Decisions section, which belongs to
    ``decision add --brief``, and a section the brief does not carry.
    """
    title = _section_title(section)
    if title is None:
        raise SpecfloError(
            f"No section {section!r} in a brief: expected one of "
            + ", ".join(repr(s) for s in SECTIONS if s != MANAGED_SECTION) + "."
        )
    if title == MANAGED_SECTION:
        raise SpecfloError(
            f"Section {title!r} is managed: use `specflo decision add --brief {brief_id}` "
            "instead of brief set."
        )
    path = brief_path(root, cfg, slug, brief_id)
    with locked(lock_path_for(root, slug, path)):
        doc = path.read_text()
        header = f"## {title}"
        if markdown.section_body(doc, header) is None:
            raise SpecfloError(f"Malformed brief {brief_id}: no '{header}' section.")
        doc = markdown.replace_section_body(doc, header, body)
        doc = markdown.bump_updated(doc, today)
        path.write_text(doc)
    return title
