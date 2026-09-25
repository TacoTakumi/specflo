"""The brief: the one document a quick-level project is worked from.

A quick project has no brainstorm, spec or plan. Its ``brief.md`` holds the
goal, the one check that says the work is done, the proof that the check
passes, and any work found along the way that the brief does not do.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from .config import SpecfloConfig
from .projects import load_project, project_dir

BRIEF_FILENAME = "brief.md"

# The brief's sections, in document order.
SECTIONS = ("Goal", "Done when", "Proof", "Deferred")

_TEMPLATE = """\
---
project: {slug}
level: quick
status: draft
created: {today}
updated: {today}
---

# Brief: {name}

## Goal
<!-- one or two sentences: what this change does -->

## Done when
<!-- exactly one check, as a single list item -->

## Proof
<!-- test or command output that shows the check passes -->

## Deferred
<!-- work found along the way that this brief does not do (optional) -->
"""


def brief_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return project_dir(root, cfg, slug) / BRIEF_FILENAME


def start_brief(
    root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
) -> tuple[Path, bool]:
    """Create the brief, or locate an existing one; ``(path, created)``.

    Never clobbers: a brief that exists is returned as it is.
    """
    project = load_project(root, cfg, slug)
    path = brief_path(root, cfg, slug)
    if path.exists():
        return path, False
    today = today or datetime.date.today().isoformat()
    path.write_text(_TEMPLATE.format(slug=project.slug, name=project.name, today=today))
    return path, True
