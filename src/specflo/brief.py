"""The brief: the one document a quick-level project is worked from.

A quick project has no brainstorm, spec or plan. Its ``brief.md`` holds the
goal, the one check that says the work is done, the proof that the check
passes, and any work found along the way that the brief does not do.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from . import markdown
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


def _list_items(body: str) -> list[str]:
    """The top-level list items of a section body: ``-``, ``*`` or ``1.`` lines."""
    return [
        line for line in body.splitlines()
        if re.match(r"^(?:[-*]|\d+[.)])\s+\S", line)
    ]


def validate_brief(root: Path, cfg: SpecfloConfig, slug: str) -> list[str]:
    """The brief's gaps; empty when it has a goal, exactly one check and proof.

    Read-only. An empty Deferred section is not a gap: it is optional.
    """
    path = brief_path(root, cfg, slug)
    if not path.is_file():
        return ["brief.md not found - a quick project makes it at `specflo new`."]
    doc = path.read_text()
    issues = []
    bodies = {}
    for title in SECTIONS:
        body = markdown.section_body(doc, f"## {title}")
        if body is None:
            issues.append(f"missing '{title}' section.")
        bodies[title] = markdown.strip_comments(body or "").strip()
    if "Goal" in bodies and not bodies["Goal"]:
        issues.append("Goal is empty: say what this change does.")
    checks = _list_items(bodies.get("Done when", ""))
    if not checks:
        issues.append("Done when has no check: write exactly one, as a list item.")
    elif len(checks) > 1:
        issues.append(
            f"Done when has {len(checks)} checks; a quick brief has exactly one."
            " Keep one and move the rest to Deferred, or move up with"
            " `specflo level fast`."
        )
    if "Proof" in bodies and not bodies["Proof"]:
        issues.append("Proof is empty: record the test or command output that shows the check passes.")
    return issues
