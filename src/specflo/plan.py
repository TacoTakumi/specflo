"""The plan artifact, its progress state machine, and their operations.

Each project gets a single ``plan.md`` next to ``spec.md``. The CLI owns the
structured, stateful parts — scaffolding, the append-only Tasks section (stable
``T-NN`` ids, supersede-as-event, required ``Implements: REQ-NN`` traceability,
dependency ordering), the per-task progress field, and read-only linting. The
plan skill writes the prose sections (Approach, Global constraints, Open
questions, Canonical refs) directly.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

from . import markdown, spec as spec_mod
from .config import SpecfloConfig
from .errors import SpecfloError
from .projects import load_project, project_dir

PLAN_FILENAME = "plan.md"

PROGRESS_STATES = ("pending", "in_progress", "done", "blocked")

_TASK_ID_RE = re.compile(r"^### (T-\d+) —", re.MULTILINE)

# Scope-reduction warning vocabulary (deferral/degradation signals), kept distinct
# from the hard placeholder terms (TODO/TBD/???) so nothing is both a hard failure
# and a soft warning, and tuned to avoid legitimate engineering language.
_SCOPE_REDUCTION_TERMS = ("v1", "simplified", "for now", "stub")

_TEMPLATE = """\
---
project: {slug}
phase: plan
status: draft
created: {today}
updated: {today}
---

# Plan: {name}

## Approach
<!-- 1–2 sentences, synthesized from the spec's Objective + the brainstorm's architecture decisions -->

## Global constraints
<!-- optional; project-wide invariants copied verbatim from the spec, implicitly part of every task -->

## Tasks
<!-- append-only; managed by `specflo task add`. Stable IDs T-NN. -->

## Open questions
<!-- required section (may say "none") -->

## Canonical refs
<!-- full paths the plan leaned on -->
"""


@dataclass
class Task:
    id: str
    text: str
    acceptance: str
    verify: str
    implements: list[str]
    depends_on: list[str]
    files: str | None
    scope: str | None
    progress: str
    status: str
    supersedes: str | None = None
    blocked: str | None = None


def plan_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return project_dir(root, cfg, slug) / PLAN_FILENAME


def start_plan(
    root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
) -> tuple[Path, bool]:
    """Create the plan artifact, or locate an existing one (resume-friendly)."""
    project = load_project(root, cfg, slug)  # raises SpecfloError if missing
    path = plan_path(root, cfg, slug)
    if path.exists():
        return path, False
    today = today or datetime.date.today().isoformat()
    path.write_text(_TEMPLATE.format(slug=project.slug, name=project.name, today=today))
    return path, True
