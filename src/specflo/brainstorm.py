"""The brainstorm artifact and its operations.

Each project gets a single ``brainstorm.md`` next to ``project.md``. The CLI owns
the structured, stateful parts of this file — scaffolding, the append-only
Decisions section (stable ``D-NN`` IDs, supersede-as-event), and read-only
linting. The brainstorm skill writes the prose sections (Current understanding,
Out of scope, Open questions, Canonical refs) directly.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

from .config import SpecfloConfig
from .errors import SpecfloError
from .projects import load_project, project_dir

BRAINSTORM_FILENAME = "brainstorm.md"

_TEMPLATE = """\
---
project: {slug}
phase: brainstorm
status: draft
created: {today}
updated: {today}
---

# Brainstorm: {name}

## Current understanding
<!-- rewritten as it converges; the synthesis the spec phase reads -->

## Decisions
<!-- append-only; managed by `specflo decision add`. Stable IDs D-NN. -->

## Out of scope / Deferred
<!-- required, must be non-empty before validate passes -->

## Open questions
<!-- required section (may say "none") -->

## Canonical refs
<!-- full paths the brainstorm leaned on -->
"""


def brainstorm_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return project_dir(root, cfg, slug) / BRAINSTORM_FILENAME


def start_brainstorm(
    root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
) -> tuple[Path, bool]:
    """Create the brainstorm artifact, or locate an existing one.

    Returns ``(path, created)``; ``created`` is False if the file already existed
    (resume-friendly — never clobbers).
    """
    project = load_project(root, cfg, slug)  # raises SpecfloError if missing
    path = brainstorm_path(root, cfg, slug)
    if path.exists():
        return path, False
    today = today or datetime.date.today().isoformat()
    path.write_text(
        _TEMPLATE.format(slug=project.slug, name=project.name, today=today)
    )
    return path, True
