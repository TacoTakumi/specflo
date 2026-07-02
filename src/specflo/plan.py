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
from dataclasses import dataclass, replace
from pathlib import Path

from . import markdown, spec as spec_mod
from .config import SpecfloConfig
from .errors import SpecfloError
from .projects import load_project, project_dir

PLAN_FILENAME = "plan.md"

PROGRESS_STATES = ("pending", "in_progress", "done", "blocked")

_TASK_ID_RE = re.compile(r"^### (T-\d+) —", re.MULTILINE)
_MILESTONE_ID_RE = re.compile(r"^### (M-\d+) —")

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
    superseded_by: str | None = None
    blocked: str | None = None
    milestone: str | None = None


@dataclass
class Milestone:
    id: str
    title: str
    exit_items: list[str]


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
        # A task is superseded if it carries the new bidirectional `Superseded by:`
        # field or only the legacy `Status: superseded by <id>` marker. The new
        # field is canonical; fall back to parsing the id out of the legacy line.
        superseded_by = fields.get("Superseded by")
        if superseded_by is None and "superseded by" in status_raw:
            m = re.search(r"superseded by\s+(\S+)", status_raw)
            superseded_by = m.group(1) if m else None
        is_superseded = superseded_by is not None or "superseded by" in status_raw
        tasks.append(Task(
            id=task_id, text=title,
            acceptance=fields.get("Acceptance", ""),
            verify=fields.get("Verify", ""),
            implements=_split_refs(fields.get("Implements", "")),
            depends_on=_split_refs(fields.get("Depends on", "")),
            files=fields.get("Files"), scope=fields.get("Scope"),
            progress=fields.get("Progress", "pending"),
            status="superseded" if is_superseded else "active",
            supersedes=fields.get("Supersedes"),
            superseded_by=superseded_by,
            blocked=fields.get("Blocked"),
            milestone=fields.get("Milestone"),
        ))
    return tasks


def _parse_exit_items(entry_lines: list[str]) -> list[str]:
    """Extract a milestone's Exit checklist items from its entry lines.

    The block opens at a ``- Exit:`` line (an inline ``- Exit: item`` counts too)
    and its items are the indented ``  - item`` lines that follow, until a
    base-indent line (a new ``- Field:`` or a header) closes it. Blank lines are
    skipped so a stray gap does not truncate a hand-edited checklist.
    """
    items: list[str] = []
    in_exit = False
    for ln in entry_lines:
        stripped = ln.strip()
        if not in_exit:
            if stripped == "- Exit:" or stripped.startswith("- Exit: "):
                in_exit = True
                rest = stripped[len("- Exit:"):].strip()
                if rest:
                    items.append(rest)
            continue
        if not (ln.startswith(" ") or ln.startswith("\t")):
            if stripped == "":
                continue
            break  # base-indent field or header closes the Exit block
        m = re.match(r"^\s+-\s+(.*\S)\s*$", ln)
        if m:
            items.append(m.group(1))
    return items


def _parse_milestones(doc: str) -> list[Milestone]:
    """Parse the ordered ``## Milestones`` entries from *doc* (empty when absent)."""
    body = markdown.section_body(doc, "## Milestones")
    if body is None:
        return []
    lines = body.splitlines(keepends=True)
    heads: list[tuple[int, str, str]] = []
    for i, line, in_fence in markdown.iter_lines_with_fence(body):
        if in_fence:
            continue
        m = _MILESTONE_ID_RE.match(line)
        if m:
            title = line.split("—", 1)[1].strip()
            heads.append((i, m.group(1), title))
    milestones: list[Milestone] = []
    for n, (start, mid, title) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        milestones.append(
            Milestone(id=mid, title=title, exit_items=_parse_exit_items(lines[start + 1:end]))
        )
    return milestones


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

    # Milestone rules are dormant until at least one milestone exists (REQ-04);
    # then membership is all-or-nothing (REQ-08), every milestone needs ≥1 member
    # task (REQ-09) and a non-empty Exit checklist (REQ-10), and no task may cite
    # an undefined milestone.
    milestones = _parse_milestones(doc)
    if milestones:
        m_ids = {m.id for m in milestones}
        members: dict[str, list[str]] = {m.id: [] for m in milestones}
        for t in active:
            if not t.milestone:
                issues.append(
                    f"{t.id} has no milestone — every task must belong to one when "
                    f"milestones exist (assign via `specflo task set-milestone`)."
                )
            elif t.milestone not in m_ids:
                issues.append(
                    f"{t.id} references milestone {t.milestone}, which is not defined "
                    f"in ## Milestones."
                )
            else:
                members[t.milestone].append(t.id)
        for m in milestones:
            if not members[m.id]:
                issues.append(f"{m.id} has no member tasks (empty milestone).")
            if not m.exit_items:
                issues.append(f"{m.id} has an empty Exit checklist (needs at least one item).")

        # Backward-only dependency invariant (REQ-11): no task may depend on a task
        # in a later milestone. Milestone order is document order; deps whose
        # milestone is unknown/missing are left to the membership/reference checks.
        order = {m.id: i for i, m in enumerate(milestones)}
        task_ms = {t.id: t.milestone for t in active}
        for t in active:
            if t.milestone not in order:
                continue
            for dep in t.depends_on:
                dep_ms = task_ms.get(dep)
                if dep_ms in order and order[dep_ms] > order[t.milestone]:
                    issues.append(
                        f"{t.id} (in {t.milestone}) depends on {dep} (in {dep_ms}), "
                        f"which is a later milestone; dependencies must not point forward."
                    )

        # The union of all milestones' derived REQ coverage must equal the active
        # REQ set (REQ-12): every active requirement is implemented by some task
        # that belongs to a milestone. (sp/active_reqs come from the coverage block
        # above; active_reqs is bound iff the spec file exists.)
        if sp.is_file():
            milestone_reqs = {r for t in active if t.milestone in order for r in t.implements}
            for req in active_reqs:
                if req not in milestone_reqs:
                    issues.append(f"{req} is not covered by any milestone's tasks.")

    if markdown.section_body(doc, "## Open questions") is None:
        issues.append("missing 'Open questions' section.")

    return issues


def reconcile_issues(root: Path, cfg: SpecfloConfig, slug: str) -> list[str]:
    """Issues blocking execute-phase completion: the plan must still validate AND
    every active task must be done. Empty == ready to complete the project."""
    issues = validate_plan(root, cfg, slug)
    if issues:
        return issues
    active = [
        t for t in _parse_tasks(plan_path(root, cfg, slug).read_text())
        if t.status == "active"
    ]
    not_done = [t.id for t in active if t.progress != "done"]
    if not_done:
        issues.append(
            "not all tasks are done: " + ", ".join(not_done)
            + " (every task must be done before completing execute)."
        )
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
    milestone: str | None = None,
    today: str | None = None,
) -> Task:
    """Append a task to the Tasks section and return it.

    Mints the next ``T-NN``. ``acceptance``/``verify`` are mandatory; ``implements``
    must name ≥1 active requirement in ``spec.md``. ``depends_on`` and
    ``supersedes`` must reference existing tasks. ``milestone``, when given, must
    name a milestone present in ``## Milestones`` and is written as the task's
    single ``- Milestone:`` field.
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

    if milestone is not None and milestone not in {m.id for m in _parse_milestones(doc)}:
        raise SpecfloError(
            f"No milestone {milestone} in this plan (add it with `specflo milestone add`)."
        )

    new_id = markdown.next_id(doc, "T-")
    if supersedes is not None:
        # Tidy the superseded task: legacy Status marker (back-compat) plus the
        # canonical bidirectional `Superseded by:` field, and reset its Progress
        # so a half-done task does not linger as in_progress.
        doc = markdown.mark_superseded(doc, supersedes, new_id)
        doc = markdown.set_entry_field(doc, supersedes, "Superseded by", new_id)
        doc = markdown.set_entry_field(doc, supersedes, "Progress", "pending")

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
    if milestone is not None:
        entry_lines.append(f"- Milestone: {milestone}")
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
        milestone=milestone,
    )


def add_milestone(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    text: str,
    exit_items: list[str],
    today: str | None = None,
) -> Milestone:
    """Append a milestone (``M-NN``) to the plan and return it.

    Creates the ``## Milestones`` section on demand (immediately before
    ``## Tasks``) so a zero-milestone plan stays byte-identical to today's. The
    ``exit_items`` list must contain at least one non-blank authored string
    (REQ-05). Touches only ``plan.md`` — never ``spec.md`` (REQ-01).
    """
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    doc = path.read_text()
    if "## Tasks" not in doc:
        raise SpecfloError("Malformed plan.md: no '## Tasks' section.")

    exit_items = [item.strip() for item in (exit_items or []) if item.strip()]
    if not exit_items:
        raise SpecfloError("A milestone needs at least one --exit checklist item.")

    doc = markdown.ensure_section_before(doc, "## Milestones", "## Tasks")
    new_id = markdown.next_id(doc, "M-")
    entry_lines = [f"### {new_id} — {text}", "- Exit:"]
    entry_lines += [f"  - {item}" for item in exit_items]
    entry = "\n".join(entry_lines) + "\n"

    doc = markdown.append_to_section(doc, "## Milestones", entry)
    doc = markdown.bump_updated(doc, today)
    path.write_text(doc)
    return Milestone(id=new_id, title=text, exit_items=exit_items)


def blocked_on_superseded_from_doc(doc: str) -> list[dict]:
    """Pending active tasks in *doc* blocked by a superseded dependency.

    Returns one entry per (blocked task, superseded dependency) pair, each naming
    the superseding task id, so callers (``task show``, ``status``, ``checkpoint``)
    can render rewire remediation. Empty when nothing is blocked this way.
    """
    tasks = _parse_tasks(doc)
    superseded = {t.id: t.superseded_by for t in tasks if t.status == "superseded"}
    findings: list[dict] = []
    for t in tasks:
        if t.status != "active" or t.progress != "pending":
            continue
        for dep in t.depends_on:
            if dep in superseded:
                findings.append({
                    "blocked": t.id, "dependency": dep,
                    "superseded_by": superseded[dep],
                })
    return findings


def blocked_on_superseded(root: Path, cfg: SpecfloConfig, slug: str) -> list[dict]:
    """File-backed wrapper of :func:`blocked_on_superseded_from_doc`."""
    path = plan_path(root, cfg, slug)
    return blocked_on_superseded_from_doc(path.read_text()) if path.is_file() else []


def superseded_block_remediation(blocks: list[dict]) -> list[str]:
    """One human remediation line per ``blocked_on_superseded`` finding."""
    lines: list[str] = []
    for b in blocks:
        dep, by, blocked = b["dependency"], b["superseded_by"], b["blocked"]
        if by:
            lines.append(
                f"{blocked} depends on superseded {dep} (superseded by {by}); "
                f"run: specflo task rewire --from {dep} --to {by}"
            )
        else:
            lines.append(
                f"{blocked} depends on superseded {dep} with no known replacement; "
                f"update {blocked}'s dependencies."
            )
    return lines


def stuck_next_step_from_doc(doc: str) -> str | None:
    """A rewire-remediation next-step line when *doc* is stuck on a superseded
    dependency (nothing actionable, yet a pending task depends on a superseded
    task), or None when the plan is not stuck this way."""
    if progress_from_doc(doc)["next_actionable"]:
        return None
    blocks = blocked_on_superseded_from_doc(doc)
    if not blocks:
        return None
    return "Blocked: " + " | ".join(superseded_block_remediation(blocks))


def stuck_next_step(root: Path, cfg: SpecfloConfig, slug: str) -> str | None:
    """File-backed wrapper of :func:`stuck_next_step_from_doc`."""
    path = plan_path(root, cfg, slug)
    return stuck_next_step_from_doc(path.read_text()) if path.is_file() else None


def active_dependents(
    root: Path, cfg: SpecfloConfig, slug: str, task_id: str
) -> list[str]:
    """Ids of active tasks whose Depends-on list includes *task_id*, in order."""
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        return []
    return [
        t.id for t in _parse_tasks(path.read_text())
        if t.status == "active" and task_id in t.depends_on
    ]


def rewire_dependency(
    root: Path,
    cfg: SpecfloConfig,
    slug: str,
    from_id: str,
    to_id: str,
    today: str | None = None,
) -> list[str]:
    """Repoint every active task depending on *from_id* to depend on *to_id*.

    Returns the ids of the tasks that were changed, in document order. Active
    tasks whose Depends-on list does not include *from_id*, and all superseded
    tasks, are left untouched; a no-op redirect leaves ``plan.md`` byte-identical.
    """
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    doc = path.read_text()
    tasks = _parse_tasks(doc)
    by_id = {t.id: t for t in tasks}
    # Validate before any write so a rejected redirect leaves plan.md untouched.
    if from_id not in by_id:
        raise SpecfloError(f"No task {from_id} to rewire from.")
    if to_id == from_id:
        raise SpecfloError(f"Cannot rewire {from_id} to itself (--to must differ from --from).")
    to_task = by_id.get(to_id)
    if to_task is None:
        raise SpecfloError(f"No task {to_id} to rewire to.")
    if to_task.status != "active":
        raise SpecfloError(f"Cannot rewire to {to_id}: it is superseded (--to must be active).")

    # Compute each dependent's post-rewire deps (order-preserving dedupe so a
    # dependent already listing --to ends with a single entry, not a duplicate).
    new_deps_by_id: dict[str, list[str]] = {}
    changed: list[str] = []
    for t in tasks:
        if t.status != "active" or from_id not in t.depends_on:
            continue
        rewired: list[str] = []
        for d in t.depends_on:
            nd = to_id if d == from_id else d
            if nd not in rewired:
                rewired.append(nd)
        new_deps_by_id[t.id] = rewired
        changed.append(t.id)
    if not changed:
        return []

    # Refuse a redirect that would introduce a cycle (validate before write).
    proposed = [
        replace(t, depends_on=new_deps_by_id.get(t.id, t.depends_on))
        for t in tasks if t.status == "active"
    ]
    cycle = _find_cycle(proposed)
    if cycle:
        raise SpecfloError(
            f"Rewiring {from_id} to {to_id} would create a dependency cycle: "
            + " -> ".join(cycle) + "."
        )

    for tid in changed:
        doc = markdown.set_entry_field(doc, tid, "Depends on", ", ".join(new_deps_by_id[tid]))
    doc = markdown.bump_updated(doc, today)
    path.write_text(doc)
    return changed


def _set_progress(
    root: Path, cfg: SpecfloConfig, slug: str, task_id: str,
    progress: str, reason: str | None = None, today: str | None = None,
) -> Task:
    if progress not in PROGRESS_STATES:
        raise SpecfloError(f"Unknown progress state {progress!r}.")
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    doc = path.read_text()
    task = next((t for t in _parse_tasks(doc) if t.id == task_id), None)
    if task is None:
        raise SpecfloError(f"No task {task_id}.")
    if task.status != "active":
        raise SpecfloError(f"Task {task_id} is superseded; its progress is frozen.")
    if progress == "done" and task.progress != "in_progress":
        raise SpecfloError(
            f"{task_id} must be in_progress before it can be done "
            f"(run `specflo task start {task_id}` first)."
        )
    doc = markdown.set_entry_field(doc, task_id, "Progress", progress)
    if progress == "blocked" and reason:
        doc = markdown.set_entry_field(doc, task_id, "Blocked", reason)
    else:
        doc = markdown.clear_entry_field(doc, task_id, "Blocked")
    doc = markdown.bump_updated(doc, today)
    path.write_text(doc)
    task.progress = progress
    task.blocked = reason if progress == "blocked" else None
    return task


def start_task(root, cfg, slug, task_id, today=None) -> Task:
    return _set_progress(root, cfg, slug, task_id, "in_progress", today=today)


def done_task(root, cfg, slug, task_id, today=None) -> Task:
    return _set_progress(root, cfg, slug, task_id, "done", today=today)


def block_task(root, cfg, slug, task_id, reason=None, today=None) -> Task:
    return _set_progress(root, cfg, slug, task_id, "blocked", reason=reason, today=today)


def reopen_task(root, cfg, slug, task_id, today=None) -> Task:
    return _set_progress(root, cfg, slug, task_id, "pending", today=today)


def set_milestone(
    root: Path, cfg: SpecfloConfig, slug: str, task_id: str,
    milestone_id: str, today: str | None = None,
) -> Task:
    """(Re)assign an active task's milestone in place, updating its single
    ``- Milestone:`` field. The task must be active and the milestone must exist.
    """
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    doc = path.read_text()
    task = next((t for t in _parse_tasks(doc) if t.id == task_id), None)
    if task is None:
        raise SpecfloError(f"No task {task_id}.")
    if task.status != "active":
        raise SpecfloError(f"Task {task_id} is superseded; its milestone is frozen.")
    if milestone_id not in {m.id for m in _parse_milestones(doc)}:
        raise SpecfloError(
            f"No milestone {milestone_id} in this plan (add it with `specflo milestone add`)."
        )
    doc = markdown.set_entry_field(doc, task_id, "Milestone", milestone_id)
    doc = markdown.bump_updated(doc, today)
    path.write_text(doc)
    task.milestone = milestone_id
    return task


def _progress_from_tasks(active: list[Task]) -> dict:
    by_state = {s: 0 for s in PROGRESS_STATES}
    for t in active:
        by_state[t.progress if t.progress in by_state else "pending"] += 1
    done_ids = {t.id for t in active if t.progress == "done"}
    next_actionable = [
        t.id for t in active
        if t.progress == "pending" and all(d in done_ids for d in t.depends_on)
    ]
    total = len(active)
    return {
        "total": total,
        "by_state": by_state,
        "done": by_state["done"],
        "next_actionable": next_actionable,
        "all_done": total > 0 and by_state["done"] == total,
    }


def progress_from_doc(doc: str) -> dict:
    return _progress_from_tasks([t for t in _parse_tasks(doc) if t.status == "active"])


def plan_progress(root: Path, cfg: SpecfloConfig, slug: str) -> dict:
    path = plan_path(root, cfg, slug)
    return progress_from_doc(path.read_text() if path.is_file() else "")


def _milestone_rollups(milestones: list[Milestone], active: list[Task]) -> list[dict]:
    """Per-milestone rollup in document order, derived purely from member tasks.

    A milestone is ``complete`` iff it has ≥1 member task and all are done (REQ-06);
    an empty milestone is never complete (validate flags it separately, REQ-09).
    """
    rollups: list[dict] = []
    for m in milestones:
        members = [t.id for t in active if t.milestone == m.id]
        done = sum(1 for t in active if t.milestone == m.id and t.progress == "done")
        total = len(members)
        rollups.append({
            "id": m.id, "title": m.title, "exit_items": m.exit_items,
            "members": members, "done": done, "total": total,
            "complete": total > 0 and done == total,
        })
    return rollups


def _current_milestone(rollups: list[dict]) -> str | None:
    """The earliest-in-document-order incomplete milestone id, or None if all are
    complete (or there are no milestones)."""
    for r in rollups:
        if not r["complete"]:
            return r["id"]
    return None


def _default_actionable(active: list[Task], milestones: list[Milestone]) -> str | None:
    """The task ``task show`` defaults to: among dependency-ready pending tasks,
    steer to the current milestone (REQ-13).

    The candidate set is exactly ``next_actionable`` — every dependency-ready
    pending task — so milestones never make a ready task *unselectable*; they only
    pick *which* ready task is the default. A ready task in the current milestone
    wins; if none is ready there, the earliest-milestone ready task is offered
    (and flagged working-ahead separately). With no milestones — or all complete
    — this is today's ``next_actionable[0]`` (REQ-04 dormancy).
    """
    actionable = _progress_from_tasks(active)["next_actionable"]
    if not actionable:
        return None
    rollups = _milestone_rollups(milestones, active)
    current = _current_milestone(rollups)
    if current is None:
        return actionable[0]
    order = {r["id"]: i for i, r in enumerate(rollups)}
    by_id = {t.id: t for t in active}
    in_current = [tid for tid in actionable if by_id[tid].milestone == current]
    if in_current:
        return in_current[0]
    # Only later-milestone tasks are ready: offer the earliest, preserving
    # document order within a milestone (sorted is stable). Unassigned tasks (an
    # invalid, mixed plan) sort last.
    return sorted(actionable, key=lambda tid: order.get(by_id[tid].milestone, len(order)))[0]


def _task_working_ahead(
    task: Task, milestones: list[Milestone], active: list[Task]
) -> bool:
    """True when *task* sits in a milestone later (in document order) than the
    current one — i.e. it is dependency-ready but ahead of the milestone the plan
    is on. Always False when there are no milestones, all are complete, or the
    task is unassigned (REQ-04 dormancy)."""
    rollups = _milestone_rollups(milestones, active)
    current = _current_milestone(rollups)
    if current is None or task.milestone is None:
        return False
    order = {r["id"]: i for i, r in enumerate(rollups)}
    if task.milestone not in order:
        return False
    return order[task.milestone] > order[current]


def milestone_progress_from_doc(doc: str) -> dict:
    """Derived milestone view of *doc*: ordered rollups + the current milestone."""
    milestones = _parse_milestones(doc)
    active = [t for t in _parse_tasks(doc) if t.status == "active"]
    rollups = _milestone_rollups(milestones, active)
    return {"milestones": rollups, "current": _current_milestone(rollups)}


def milestone_progress(root: Path, cfg: SpecfloConfig, slug: str) -> dict:
    path = plan_path(root, cfg, slug)
    return milestone_progress_from_doc(path.read_text() if path.is_file() else "")


def current_milestone_from_doc(doc: str) -> dict | None:
    """The current milestone's rollup (``id``, ``title``, ``done``, ``total``, …),
    or None when the plan has no milestones or all are complete.

    The single source status and checkpoint share for their "where are we"
    milestone line, so both name the same milestone the same way (REQ-15).
    """
    view = milestone_progress_from_doc(doc)
    if view["current"] is None:
        return None
    return next(r for r in view["milestones"] if r["id"] == view["current"])


def current_milestone(root: Path, cfg: SpecfloConfig, slug: str) -> dict | None:
    path = plan_path(root, cfg, slug)
    return current_milestone_from_doc(path.read_text() if path.is_file() else "")


def milestone_boundary_from_doc(doc: str) -> dict | None:
    """The soft milestone-boundary verify beat for *doc*, or None when not at one.

    Purely derived (REQ-14) — nothing is persisted. Returns the *just-completed*
    milestone whose authored Exit checklist should be surfaced for a user-gated,
    soft proceed. The beat fires in exactly two situations:

    - **Mid-plan boundary:** the current (earliest-incomplete) milestone is
      immediately preceded by a complete milestone and none of the current
      milestone's member tasks have started yet — we sit exactly at the boundary
      the finished milestone opened. The Exit checklist surfaced is the finished
      (preceding) milestone's, with ``all_complete`` False.
    - **All complete:** every milestone is complete — the last milestone's Exit
      checklist is surfaced with ``all_complete`` True, alongside the all-done
      next step.

    Dormant (None) on a milestone-free plan, while still on the first milestone,
    and once any task of the next milestone is under way — so the beat shows once,
    at the crossing, not for the rest of the milestone.
    """
    milestones = _parse_milestones(doc)
    if not milestones:
        return None
    active = [t for t in _parse_tasks(doc) if t.status == "active"]
    rollups = _milestone_rollups(milestones, active)
    current = _current_milestone(rollups)
    if current is None:
        last = rollups[-1]
        return {
            "id": last["id"], "title": last["title"],
            "exit_items": last["exit_items"], "all_complete": True,
        }
    order = {r["id"]: i for i, r in enumerate(rollups)}
    idx = order[current]
    if idx == 0:
        return None  # still on the first milestone — no boundary crossed yet
    # We sit *at* the boundary only while no task of the current milestone has
    # started; the moment one is in progress or done we have moved past it.
    if any(
        t.milestone == current and t.progress in ("in_progress", "done")
        for t in active
    ):
        return None
    prev = rollups[idx - 1]  # current is earliest-incomplete, so idx-1 is complete
    return {
        "id": prev["id"], "title": prev["title"],
        "exit_items": prev["exit_items"], "all_complete": False,
    }


def milestone_boundary(root: Path, cfg: SpecfloConfig, slug: str) -> dict | None:
    """File-backed wrapper of :func:`milestone_boundary_from_doc`."""
    path = plan_path(root, cfg, slug)
    return milestone_boundary_from_doc(path.read_text()) if path.is_file() else None


def boundary_beat_lines(boundary: dict) -> list[str]:
    """Human-readable lines for the soft milestone-boundary verify beat, shared by
    status, checkpoint, and task show so all three phrase the beat identically
    (REQ-14). A user-gated proceed prompt that mirrors ``advance``: it surfaces the
    just-completed milestone's Exit checklist and invites — never forces — a
    proceed. Nothing here ever blocks or changes an exit code.
    """
    tail = (
        "All milestones complete — confirm this Exit checklist, then run "
        "`specflo advance` to proceed and finish the project."
        if boundary.get("all_complete")
        else "Soft check — nothing blocks. Proceed to the next milestone once satisfied."
    )
    return [
        f"Milestone {boundary['id']} ({boundary['title']}) complete — "
        "verify its Exit checklist before proceeding:",
        *(f"  - {item}" for item in boundary["exit_items"]),
        tail,
    ]


def milestone_detail_from_doc(doc: str, milestone_id: str) -> dict | None:
    """Full derived detail for one milestone, or None if it is not in *doc*.

    Bundles the authored Exit checklist, member tasks (with progress), the
    done/total rollup and derived completeness, and the milestone's REQ set —
    the sorted union of member tasks' ``Implements`` citations (REQ-12), so a REQ
    implemented in two milestones surfaces under both.
    """
    m = next((x for x in _parse_milestones(doc) if x.id == milestone_id), None)
    if m is None:
        return None
    members = [
        t for t in _parse_tasks(doc)
        if t.status == "active" and t.milestone == milestone_id
    ]
    done = sum(1 for t in members if t.progress == "done")
    total = len(members)
    reqs = sorted({r for t in members for r in t.implements})
    return {
        "id": m.id, "title": m.title, "exit_items": m.exit_items,
        "members": [{"id": t.id, "text": t.text, "progress": t.progress} for t in members],
        "done": done, "total": total,
        "complete": total > 0 and done == total,
        "reqs": reqs,
    }


def milestone_detail(
    root: Path, cfg: SpecfloConfig, slug: str, milestone_id: str
) -> dict | None:
    path = plan_path(root, cfg, slug)
    return milestone_detail_from_doc(path.read_text(), milestone_id) if path.is_file() else None


def list_tasks(
    root: Path, cfg: SpecfloConfig, slug: str, include_superseded: bool = False
) -> list[Task]:
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    tasks = _parse_tasks(path.read_text())
    if include_superseded:
        return tasks
    return [t for t in tasks if t.status == "active"]


def task_brief(
    root: Path, cfg: SpecfloConfig, slug: str, task_id: str | None = None
) -> dict:
    """Assemble the progressive-disclosure brief for one task: its own entry, the
    full text of each cited REQ-NN section, and the plan's Global constraints.

    ``task_id`` defaults to the first ``next_actionable`` task. Raises
    SpecfloError if there is no such active task.
    """
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    doc = path.read_text()
    active = [t for t in _parse_tasks(doc) if t.status == "active"]
    milestones = _parse_milestones(doc)
    if task_id is None:
        task_id = _default_actionable(active, milestones)
        if task_id is None:
            blocks = blocked_on_superseded(root, cfg, slug)
            if blocks:
                detail = "\n".join("  " + ln for ln in superseded_block_remediation(blocks))
                raise SpecfloError(
                    "No actionable task: a pending task is blocked by a superseded "
                    "dependency.\n" + detail
                )
            raise SpecfloError(
                "No actionable task (all done, or remaining tasks are blocked "
                "or waiting on dependencies). See `specflo task list`."
            )
    task = next((t for t in active if t.id == task_id), None)
    if task is None:
        raise SpecfloError(f"No active task {task_id}.")

    sp = spec_mod.spec_path(root, cfg, slug)
    spec_doc = sp.read_text() if sp.is_file() else ""
    requirements = [
        {"id": req, "section": spec_mod.requirement_section(spec_doc, req)}
        for req in task.implements
    ]
    constraints = markdown.strip_comments(
        markdown.section_body(doc, "## Global constraints") or ""
    ).strip() or None
    return {
        "task": {
            "id": task.id, "text": task.text, "acceptance": task.acceptance,
            "verify": task.verify, "implements": task.implements,
            "depends_on": task.depends_on, "files": task.files,
            "scope": task.scope, "progress": task.progress,
        },
        "requirements": requirements,
        "global_constraints": constraints,
        "working_ahead": _task_working_ahead(task, milestones, active),
        # The soft milestone-boundary verify beat (None off a boundary), so the
        # execute surface can surface the just-completed milestone's Exit checklist
        # alongside the next task (REQ-14).
        "boundary": milestone_boundary_from_doc(doc),
    }
