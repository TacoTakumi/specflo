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
from .errors import SpecfloError, refuse_duplicate, require_one_line
from . import briefs as briefs_mod, markdown
from .locking import lock_path_for, locked
from .projects import load_project, project_dir

BRAINSTORM_FILENAME = "brainstorm.md"
BRAINSTORM_SOURCE = "brainstorm"

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

## Research
<!-- findings + surprises from the research subagent (optional); links go to Canonical refs -->

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
    # Where the decision lives: "brainstorm", or the B-NN of a brief inside
    # the project. One D-NN sequence runs across all of them.
    source: str = BRAINSTORM_SOURCE
    # An approved divergence from the reference design the project follows.
    diverges: bool = False


DIVERGES_FIELD = "Diverges"
_DECISION_HEAD_RE = re.compile(r"^### (D-\d+) — (.*)$")


def decision_documents(root: Path, cfg: SpecfloConfig, slug: str) -> list[tuple[str, Path]]:
    """Every document that holds decisions, as ``(source, path)``: the
    brainstorm first, then each brief in id order."""
    documents = []
    path = brainstorm_path(root, cfg, slug)
    if path.is_file():
        documents.append((BRAINSTORM_SOURCE, path))
    documents.extend(briefs_mod.brief_files(root, cfg, slug))
    return documents


def _decision_numbers(doc: str) -> list[int]:
    return [
        int(m.group(1)[2:])
        for _, line, in_fence in markdown.iter_lines_with_fence(doc)
        if not in_fence and (m := _DECISION_HEAD_RE.match(line.rstrip("\r\n")))
    ]


def next_decision_id(root: Path, cfg: SpecfloConfig, slug: str) -> str:
    """The next D-NN across the brainstorm and every brief of the project."""
    numbers = [
        n for _, path in decision_documents(root, cfg, slug)
        for n in _decision_numbers(path.read_text())
    ]
    return f"D-{(max(numbers) if numbers else 0) + 1:02d}"


def _decision_entries(doc: str, source: str) -> list[Decision]:
    """Every decision entry of ``doc`` in document order, superseded ones included."""
    entries: list[Decision] = []
    fields: dict[str, str] = {}
    head: tuple[str, str] | None = None

    def close() -> None:
        if head is None:
            return
        status = fields.get("Status", "active")
        supersedes = fields.get("Supersedes")
        entries.append(Decision(
            id=head[0], text=head[1], rationale=fields.get("Rationale", "—"),
            supersedes=supersedes, status=status, source=source,
            diverges=fields.get(DIVERGES_FIELD, "").strip().casefold() == "yes",
        ))

    for _, raw, in_fence in markdown.iter_lines_with_fence(doc):
        line = raw.rstrip("\r\n")
        if in_fence:
            continue
        if m := _DECISION_HEAD_RE.match(line):
            close()
            head, fields = (m.group(1), m.group(2).strip()), {}
        elif line.startswith("## "):
            close()
            head, fields = None, {}
        elif head is not None and (f := re.match(r"^- ([A-Za-z ]+): (.*)$", line)):
            fields.setdefault(f.group(1), f.group(2).strip())
    close()
    return entries


def list_decisions(
    root: Path, cfg: SpecfloConfig, slug: str,
    diverges_only: bool = False, include_superseded: bool = False,
) -> list[Decision]:
    """The project's decisions across the brainstorm and every brief, in
    document order; active ones only unless ``include_superseded``."""
    found = [
        d for source, path in decision_documents(root, cfg, slug)
        for d in _decision_entries(path.read_text(), source)
    ]
    if not include_superseded:
        found = [d for d in found if "superseded by" not in d.status]
    if diverges_only:
        found = [d for d in found if d.diverges]
    return found


def _document_holding(root: Path, cfg: SpecfloConfig, slug: str, decision_id: str) -> Path | None:
    """The document that holds decision ``decision_id``, or None."""
    head = re.compile(rf"^### {re.escape(decision_id)} —", re.MULTILINE)
    for _, path in decision_documents(root, cfg, slug):
        if head.search(path.read_text()):
            return path
    return None


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
    actor: str | None = None,
    brief_id: str | None = None,
    diverges: bool = False,
) -> Decision:
    """Append a decision to a Decisions section and return it.

    The decision goes into the brainstorm, or into brief ``brief_id`` when one
    is named. Either way it takes the next ``D-NN`` across the brainstorm and
    every brief, so one sequence runs through the project. If ``supersedes``
    is given, the named decision is marked superseded in place, whichever
    document holds it, and linked from the new entry. ``diverges`` marks an
    approved divergence from the reference design. ``actor`` names the
    identity adding it, written as an ``Actor`` line; a local add passes none
    and writes none.
    """
    require_one_line("A decision's text", text)
    require_one_line("A decision's rationale", rationale)
    if brief_id is not None:
        path = briefs_mod.brief_path(root, cfg, slug, brief_id)
        where = brief_id
    else:
        path = brainstorm_path(root, cfg, slug)
        where = BRAINSTORM_SOURCE
        if not path.is_file():
            raise SpecfloError("No brainstorm yet. Run `specflo brainstorm start` first.")
    with locked(lock_path_for(root, slug, path)):
        doc = path.read_text()
        if "## Decisions" not in doc:
            raise SpecfloError(f"Malformed {path.name}: no '## Decisions' section.")

        holder = None
        if supersedes is not None:
            holder = _document_holding(root, cfg, slug, supersedes)
            if holder is None:
                raise SpecfloError(f"No decision {supersedes} to supersede.")

        active = {
            d.id: d.text for d in list_decisions(root, cfg, slug)
        }
        # Decisions live in the brainstorm and in every brief, so the hint
        # names the list that spans them when the add is into a brief.
        refuse_duplicate(
            "decision", text, active, supersedes, where,
            look="specflo decision list" if brief_id is not None else None,
        )

        new_id = next_decision_id(root, cfg, slug)
        rationale_text = rationale if rationale else "—"

        if holder is not None and holder == path:
            doc = markdown.mark_superseded(doc, supersedes, new_id)
        elif holder is not None:
            with locked(lock_path_for(root, slug, holder)):
                other = markdown.mark_superseded(holder.read_text(), supersedes, new_id)
                holder.write_text(markdown.bump_updated(other, today))

        entry_lines = [f"### {new_id} — {text}", f"- Rationale: {rationale_text}"]
        if diverges:
            entry_lines.append(f"- {DIVERGES_FIELD}: yes")
        if supersedes is not None:
            entry_lines.append(f"- Supersedes: {supersedes}")
        if actor:
            entry_lines.append(f"- Actor: {actor}")
        entry_lines.append("- Status: active")
        entry = "\n".join(entry_lines) + "\n"

        doc = markdown.append_to_section(doc, "## Decisions", entry)
        doc = markdown.bump_updated(doc, today)
        path.write_text(doc)
    return Decision(
        id=new_id,
        text=text,
        rationale=rationale_text,
        supersedes=supersedes,
        status="active",
        source=brief_id if brief_id is not None else BRAINSTORM_SOURCE,
        diverges=diverges,
    )


def complete_brainstorm(
    root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
) -> None:
    """Mark the brainstorm complete (``status: draft → complete``); bump ``updated``.

    Called when leaving the brainstorm phase (by `specflo advance`). Raises
    ``SpecfloError`` if the artifact is missing.
    """
    path = brainstorm_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No brainstorm yet. Run `specflo brainstorm start` first.")
    with locked(lock_path_for(root, slug, path)):
        doc = path.read_text()
        # Frontmatter `status:` only — the leading-`-` decision `- Status:` lines and
        # the count=1 (frontmatter comes first) keep this from touching entries.
        doc = re.sub(r"(?m)^status:.*$", "status: complete", doc, count=1)
        doc = markdown.bump_updated(doc, today)
        path.write_text(doc)


def validate_brainstorm(root: Path, cfg: SpecfloConfig, slug: str) -> list[str]:
    """Return a list of lint issues (empty == ready). Read-only."""
    path = brainstorm_path(root, cfg, slug)
    if not path.is_file():
        return ["brainstorm.md not found — run `specflo brainstorm start`."]
    doc = path.read_text()
    body = markdown.strip_comments(doc)
    issues = markdown.placeholder_issues(body)

    if not _DECISION_ID_RE.search(doc):
        issues.append("no decisions captured (need at least one).")

    out_of_scope = markdown.section_body(doc, "## Out of scope / Deferred")
    if out_of_scope is None:
        issues.append("missing 'Out of scope / Deferred' section.")
    elif not markdown.strip_comments(out_of_scope).strip():
        issues.append("'Out of scope / Deferred' section is empty.")

    if markdown.section_body(doc, "## Open questions") is None:
        issues.append("missing 'Open questions' section.")

    return issues


def active_decision_ids(doc: str) -> list[str]:
    """The ids of the decisions no later decision supersedes, in document order."""
    lines = doc.splitlines(keepends=True)
    heads = [
        (i, m.group(1))
        for i, line, in_fence in markdown.iter_lines_with_fence(doc)
        if not in_fence and (m := _DECISION_ID_RE.match(line))
    ]
    active = []
    for n, (start, decision_id) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        block = []
        for line in lines[start + 1:end]:
            if line.startswith("## "):
                break
            block.append(line)
        status = next((ln for ln in block if ln.startswith("- Status:")), "")
        if "superseded by" not in status:
            active.append(decision_id)
    return active
