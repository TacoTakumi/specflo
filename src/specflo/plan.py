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


def _split_refs(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_tasks(doc: str) -> list[Task]:
    """Parse all task entries (active and superseded) from *doc*, in order."""
    lines = doc.splitlines(keepends=True)
    heads: list[tuple[int, str, str]] = []
    for i, line, in_fence in markdown.iter_lines_with_fence(doc):
        if in_fence:
            continue
        m = _TASK_ID_RE.match(line)
        if m:
            title = line.split("—", 1)[1].strip()
            heads.append((i, m.group(1), title))
    tasks: list[Task] = []
    for n, (start, task_id, title) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        for i in range(start + 1, end):
            if lines[i].startswith("## "):
                end = i
                break
        fields: dict[str, str] = {}
        for ln in lines[start + 1:end]:
            if ln.startswith("- ") and ":" in ln:
                key, _, val = ln[2:].partition(":")
                fields[key.strip()] = val.strip()
        status_raw = fields.get("Status", "")
        tasks.append(Task(
            id=task_id, text=title,
            acceptance=fields.get("Acceptance", ""),
            verify=fields.get("Verify", ""),
            implements=_split_refs(fields.get("Implements", "")),
            depends_on=_split_refs(fields.get("Depends on", "")),
            files=fields.get("Files"), scope=fields.get("Scope"),
            progress=fields.get("Progress", "pending"),
            status="superseded" if "superseded by" in status_raw else "active",
            supersedes=fields.get("Supersedes"),
            blocked=fields.get("Blocked"),
        ))
    return tasks


def _find_cycle(tasks: list[Task]) -> list[str] | None:
    ids = {t.id for t in tasks}
    graph = {t.id: [d for d in t.depends_on if d in ids] for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in graph}
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt, WHITE) == GRAY:
                return stack[stack.index(nxt):] + [nxt]
            if color.get(nxt, WHITE) == WHITE:
                found = dfs(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for tid in graph:
        if color[tid] == WHITE:
            found = dfs(tid)
            if found:
                return found
    return None


def validate_plan(root: Path, cfg: SpecfloConfig, slug: str) -> list[str]:
    """Return a list of blocking lint issues (empty == ready). Read-only."""
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        return ["plan.md not found — run `specflo plan start`."]
    doc = path.read_text()
    issues = markdown.placeholder_issues(markdown.strip_comments(doc))

    active = [t for t in _parse_tasks(doc) if t.status == "active"]
    if not active:
        issues.append("no tasks captured (need at least one).")
        return issues

    for t in active:
        if not t.acceptance:
            issues.append(f"{t.id} has no acceptance criterion.")
        if not t.verify:
            issues.append(f"{t.id} has no verification step.")

    sp = spec_mod.spec_path(root, cfg, slug)
    if not sp.is_file():
        issues.append("spec.md not found — coverage cannot be checked.")
    else:
        active_reqs = spec_mod.active_requirement_ids(sp.read_text())
        covered: set[str] = set()
        for t in active:
            if not t.implements:
                issues.append(f"{t.id} implements no requirement (needs Implements: REQ-NN).")
            for req in t.implements:
                if req not in active_reqs:
                    issues.append(f"{t.id} implements {req}, which is not an active requirement.")
                else:
                    covered.add(req)
        for req in active_reqs:
            if req not in covered:
                issues.append(f"{req} is not implemented by any task.")

    ids = {t.id for t in active}
    for t in active:
        for dep in t.depends_on:
            if dep not in ids:
                issues.append(f"{t.id} depends on {dep}, which is not an active task.")
    cycle = _find_cycle(active)
    if cycle:
        issues.append(f"dependency cycle: {' -> '.join(cycle)}.")

    if markdown.section_body(doc, "## Open questions") is None:
        issues.append("missing 'Open questions' section.")

    return issues


def plan_warnings(root: Path, cfg: SpecfloConfig, slug: str) -> list[str]:
    """Return non-blocking scope-reduction warnings for active tasks."""
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        return []
    warnings: list[str] = []
    for t in _parse_tasks(path.read_text()):
        if t.status != "active":
            continue
        haystack = f"{t.text} {t.acceptance} {t.verify}".lower()
        for term in _SCOPE_REDUCTION_TERMS:
            if re.search(rf"\b{re.escape(term)}\b", haystack):
                warnings.append(
                    f'{t.id} may reduce scope ("{term}") — deliver what the requirement needs, or split.'
                )
    return warnings


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


def _active_requirement_ids(root: Path, cfg: SpecfloConfig, slug: str) -> list[str]:
    sp = spec_mod.spec_path(root, cfg, slug)
    if not sp.is_file():
        raise SpecfloError("Cannot link requirements: no spec.md for this project.")
    return spec_mod.active_requirement_ids(sp.read_text())


def complete_plan(
    root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
) -> None:
    """Mark the plan complete (``status: draft → complete``); bump ``updated``."""
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    doc = path.read_text()
    doc = re.sub(r"(?m)^status:.*$", "status: complete", doc, count=1)
    doc = markdown.bump_updated(doc, today)
    path.write_text(doc)


def add_task(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    text: str,
    acceptance: str,
    verify: str,
    implements: list[str],
    depends_on: list[str] | None = None,
    files: str | None = None,
    scope: str | None = None,
    supersedes: str | None = None,
    today: str | None = None,
) -> Task:
    """Append a task to the Tasks section and return it.

    Mints the next ``T-NN``. ``acceptance``/``verify`` are mandatory; ``implements``
    must name ≥1 active requirement in ``spec.md``. ``depends_on`` and
    ``supersedes`` must reference existing tasks.
    """
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    doc = path.read_text()
    if "## Tasks" not in doc:
        raise SpecfloError("Malformed plan.md: no '## Tasks' section.")

    depends_on = depends_on or []
    if not implements:
        raise SpecfloError("A task must implement at least one requirement (--from REQ-NN).")

    active_reqs = _active_requirement_ids(root, cfg, slug)
    for req_id in implements:
        if req_id not in active_reqs:
            raise SpecfloError(
                f"Cannot implement {req_id}: not an active requirement in spec.md."
            )

    for dep in depends_on:
        if not re.search(rf"^### {re.escape(dep)} —", doc, re.MULTILINE):
            raise SpecfloError(f"No task {dep} to depend on.")

    if supersedes is not None and not re.search(
        rf"^### {re.escape(supersedes)} —", doc, re.MULTILINE
    ):
        raise SpecfloError(f"No task {supersedes} to supersede.")

    new_id = markdown.next_id(doc, "T-")
    if supersedes is not None:
        doc = markdown.mark_superseded(doc, supersedes, new_id)

    entry_lines = [
        f"### {new_id} — {text}",
        f"- Acceptance: {acceptance}",
        f"- Verify: {verify}",
        f"- Implements: {', '.join(implements)}",
    ]
    if depends_on:
        entry_lines.append(f"- Depends on: {', '.join(depends_on)}")
    if files:
        entry_lines.append(f"- Files: {files}")
    if scope:
        entry_lines.append(f"- Scope: {scope}")
    if supersedes is not None:
        entry_lines.append(f"- Supersedes: {supersedes}")
    entry_lines.append("- Progress: pending")
    entry_lines.append("- Status: active")
    entry = "\n".join(entry_lines) + "\n"

    doc = markdown.append_to_section(doc, "## Tasks", entry)
    doc = markdown.bump_updated(doc, today)
    path.write_text(doc)
    return Task(
        id=new_id, text=text, acceptance=acceptance, verify=verify,
        implements=implements, depends_on=depends_on, files=files, scope=scope,
        progress="pending", status="active", supersedes=supersedes,
    )
