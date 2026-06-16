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

_DECISION_ID_RE = re.compile(r"^### (D-\d+) —", re.MULTILINE)

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


@dataclass
class Decision:
    id: str
    text: str
    rationale: str
    supersedes: str | None
    status: str


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


def add_decision(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    text: str,
    rationale: str | None = None,
    supersedes: str | None = None,
    today: str | None = None,
) -> Decision:
    """Append a decision to the Decisions section and return it.

    Assigns the next ``D-NN`` id. If ``supersedes`` is given, the named decision
    is marked superseded (kept in place) and linked from the new entry.
    """
    path = brainstorm_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No brainstorm yet. Run `specflo brainstorm start` first.")
    doc = path.read_text()
    if "## Decisions" not in doc:
        raise SpecfloError("Malformed brainstorm.md: no '## Decisions' section.")

    if supersedes is not None and not re.search(
        rf"^### {re.escape(supersedes)} —", doc, re.MULTILINE
    ):
        raise SpecfloError(f"No decision {supersedes} to supersede.")

    new_id = _next_decision_id(doc)
    rationale_text = rationale if rationale else "—"

    if supersedes is not None:
        doc = _mark_superseded(doc, supersedes, new_id)

    entry_lines = [f"### {new_id} — {text}", f"- Rationale: {rationale_text}"]
    if supersedes is not None:
        entry_lines.append(f"- Supersedes: {supersedes}")
    entry_lines.append("- Status: active")
    entry = "\n".join(entry_lines) + "\n"

    doc = _append_to_section(doc, "## Decisions", entry)
    doc = _bump_updated(doc, today)
    path.write_text(doc)
    return Decision(
        id=new_id,
        text=text,
        rationale=rationale_text,
        supersedes=supersedes,
        status="active",
    )


# --- internal helpers ---


def _next_decision_id(doc: str) -> str:
    numbers = [int(m.group(1).split("-")[1]) for m in _DECISION_ID_RE.finditer(doc)]
    nxt = max(numbers) + 1 if numbers else 1
    return f"D-{nxt:02d}"


def _append_to_section(doc: str, header: str, entry: str) -> str:
    lines = doc.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.strip() == header)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    lines.insert(end, entry + "\n")  # trailing blank line separates entries
    return "".join(lines)


def _mark_superseded(doc: str, dec_id: str, by_id: str) -> str:
    lines = doc.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith(f"### {dec_id} —"))
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("### ") or lines[i].startswith("## "):
            break
        if lines[i].startswith("- Status:"):
            lines[i] = f"- Status: superseded by {by_id}\n"
            break
    return "".join(lines)


def _bump_updated(doc: str, today: str | None = None) -> str:
    today = today or datetime.date.today().isoformat()
    return re.sub(r"(?m)^updated:.*$", f"updated: {today}", doc, count=1)
