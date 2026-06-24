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


def complete_spec(
    root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
) -> None:
    """Mark the spec complete (``status: draft → complete``); bump ``updated``.

    Called when leaving the spec phase (by `specflo advance`). Raises
    ``SpecfloError`` if the artifact is missing.
    """
    path = spec_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No spec yet. Run `specflo spec start` first.")
    doc = path.read_text()
    # Frontmatter `status:` only — leading-`-` requirement `- Status:` lines and
    # count=1 (frontmatter comes first) keep this from touching entries.
    doc = re.sub(r"(?m)^status:.*$", "status: complete", doc, count=1)
    doc = markdown.bump_updated(doc, today)
    path.write_text(doc)


def validate_spec(root: Path, cfg: SpecfloConfig, slug: str) -> list[str]:
    """Return a list of lint issues (empty == ready). Read-only."""
    path = spec_path(root, cfg, slug)
    if not path.is_file():
        return ["spec.md not found — run `specflo spec start`."]
    doc = path.read_text()
    body = markdown.strip_comments(doc)
    issues = markdown.placeholder_issues(body)

    if not _REQ_ID_RE.search(doc):
        issues.append("no requirements captured (need at least one).")
    else:
        issues.extend(_requirements_without_acceptance(doc))

    in_scope = markdown.section_body(doc, "### In scope")
    if in_scope is None:
        issues.append("missing 'In scope' section.")
    elif not markdown.strip_comments(in_scope).strip():
        issues.append("'In scope' section is empty.")

    out_scope = markdown.section_body(doc, "### Out of scope")
    if out_scope is None:
        issues.append("missing 'Out of scope' section.")
    elif not markdown.strip_comments(out_scope).strip():
        issues.append("'Out of scope' section is empty.")

    if markdown.section_body(doc, "## Open questions") is None:
        issues.append("missing 'Open questions' section.")

    return issues


def _requirements_without_acceptance(doc: str) -> list[str]:
    """Return an issue per ACTIVE requirement that lacks a non-empty Acceptance."""
    lines = doc.splitlines(keepends=True)
    heads: list[tuple[int, str]] = []
    for i, line, in_fence in markdown.iter_lines_with_fence(doc):
        if in_fence:
            continue
        m = _REQ_ID_RE.match(line)
        if m:
            heads.append((i, m.group(1)))

    issues: list[str] = []
    for n, (start, req_id) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        for i in range(start + 1, end):  # also stop at the next H2 boundary
            if lines[i].startswith("## "):
                end = i
                break
        block = lines[start:end]
        status = next((ln for ln in block if ln.startswith("- Status:")), "")
        if "superseded by" in status:
            continue  # historical — not re-checked
        acceptance = next((ln for ln in block if ln.startswith("- Acceptance:")), None)
        if acceptance is None or not acceptance.split(":", 1)[1].strip():
            issues.append(f"{req_id} has no acceptance criterion.")
    return issues


def active_requirement_ids(doc: str) -> list[str]:
    """Return the ids of ACTIVE (non-superseded) requirements, in document order."""
    lines = doc.splitlines(keepends=True)
    heads: list[tuple[int, str]] = []
    for i, line, in_fence in markdown.iter_lines_with_fence(doc):
        if in_fence:
            continue
        m = _REQ_ID_RE.match(line)
        if m:
            heads.append((i, m.group(1)))
    active: list[str] = []
    for n, (start, req_id) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        for i in range(start + 1, end):
            if lines[i].startswith("## "):
                end = i
                break
        block = lines[start:end]
        status = next((ln for ln in block if ln.startswith("- Status:")), "")
        if "superseded by" in status:
            continue
        active.append(req_id)
    return active


def requirement_section(doc: str, req_id: str) -> str | None:
    """Return the ``### {req_id} —`` entry block (header + its ``- `` field
    lines), or None if absent. Fence-aware; stops at the next ``###``/``## ``."""
    lines = doc.splitlines(keepends=True)
    start = None
    for i, line, in_fence in markdown.iter_lines_with_fence(doc):
        if not in_fence and line.startswith(f"### {req_id} —"):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("### ") or lines[i].startswith("## "):
            end = i
            break
    return "".join(lines[start:end]).rstrip() + "\n"
