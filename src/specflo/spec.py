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


@dataclass
class Requirement:
    id: str
    text: str
    acceptance: str
    derives_from: str | None
    supersedes: str | None
    status: str


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


def add_requirement(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    text: str,
    acceptance: str,
    derives_from: str | None = None,
    supersedes: str | None = None,
    today: str | None = None,
) -> Requirement:
    """Append a requirement to the Requirements section and return it.

    Assigns the next ``REQ-NN`` id. ``acceptance`` is mandatory. If
    ``derives_from`` is given it must name a decision present in the project's
    ``brainstorm.md``. If ``supersedes`` is given, the named requirement is
    marked superseded (kept in place) and linked from the new entry.
    """
    path = spec_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No spec yet. Run `specflo spec start` first.")
    doc = path.read_text()
    if "## Requirements" not in doc:
        raise SpecfloError("Malformed spec.md: no '## Requirements' section.")

    if supersedes is not None and not re.search(
        rf"^### {re.escape(supersedes)} —", doc, re.MULTILINE
    ):
        raise SpecfloError(f"No requirement {supersedes} to supersede.")

    if derives_from is not None:
        _require_decision_exists(root, cfg, slug, derives_from)

    new_id = markdown.next_id(doc, "REQ-")

    if supersedes is not None:
        doc = markdown.mark_superseded(doc, supersedes, new_id)

    entry_lines = [f"### {new_id} — {text}", f"- Acceptance: {acceptance}"]
    if derives_from is not None:
        entry_lines.append(f"- Derives from: {derives_from}")
    if supersedes is not None:
        entry_lines.append(f"- Supersedes: {supersedes}")
    entry_lines.append("- Status: active")
    entry = "\n".join(entry_lines) + "\n"

    doc = markdown.append_to_section(doc, "## Requirements", entry)
    doc = markdown.bump_updated(doc, today)
    path.write_text(doc)
    return Requirement(
        id=new_id,
        text=text,
        acceptance=acceptance,
        derives_from=derives_from,
        supersedes=supersedes,
        status="active",
    )


def _require_decision_exists(
    root: Path, cfg: SpecfloConfig, slug: str, dec_id: str
) -> None:
    """Raise SpecfloError unless ``dec_id`` is a decision in the brainstorm."""
    bpath = brainstorm_path(root, cfg, slug)
    if not bpath.is_file():
        raise SpecfloError(f"Cannot link {dec_id}: no brainstorm.md for this project.")
    if not re.search(rf"^### {re.escape(dec_id)} —", bpath.read_text(), re.MULTILINE):
        raise SpecfloError(f"No decision {dec_id} in the brainstorm to derive from.")
