"""The spec artifact and its operations.

Each project gets a single ``spec.md`` next to ``brainstorm.md``. The CLI owns
the structured, stateful parts — scaffolding, the append-only Requirements
section (stable ``REQ-NN`` IDs, supersede-as-event, optional ``Derives from``
links to brainstorm decisions), and read-only linting. The spec skill writes the
prose sections (Objective, Boundaries, Open questions, Canonical refs) directly.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

from . import markdown
from .brainstorm import brainstorm_path
from .config import SpecfloConfig
from .errors import SpecfloError
from .projects import load_project, project_dir

SPEC_FILENAME = "spec.md"

_REQ_ID_RE = re.compile(r"^### (REQ-\d+) —", re.MULTILINE)

_TEMPLATE = """\
---
project: {slug}
phase: spec
status: draft
created: {today}
updated: {today}
---

# Spec: {name}

## Objective
<!-- 1–2 sentences, synthesized from the brainstorm's Current understanding -->

## Requirements
<!-- append-only; managed by `specflo requirement add`. Stable IDs REQ-NN. -->

## Boundaries
### In scope
<!-- required, non-empty -->

### Out of scope
<!-- required, non-empty; carried from the brainstorm's Out of scope / Deferred -->

## Open questions
<!-- required section (may say "none") -->

## Canonical refs
<!-- full paths the spec leaned on -->
"""


def spec_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return project_dir(root, cfg, slug) / SPEC_FILENAME


def start_spec(
    root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
) -> tuple[Path, bool]:
    """Create the spec artifact, or locate an existing one.

    Returns ``(path, created)``; ``created`` is False if the file already existed
    (resume-friendly — never clobbers).
    """
    project = load_project(root, cfg, slug)  # raises SpecfloError if missing
    path = spec_path(root, cfg, slug)
    if path.exists():
        return path, False
    today = today or datetime.date.today().isoformat()
    path.write_text(
        _TEMPLATE.format(slug=project.slug, name=project.name, today=today)
    )
    return path, True
