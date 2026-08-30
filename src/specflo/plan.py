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
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import markdown, spec as spec_mod
from .config import SpecfloConfig
from .errors import SpecfloError
from .locking import lock_path_for, locked
from .projects import FAN_OUT_EXECUTION, load_project, project_dir

PLAN_FILENAME = "plan.md"

PROGRESS_STATES = ("pending", "in_progress", "done", "blocked")

_TASK_ID_RE = re.compile(r"^### (T-\d+) —", re.MULTILINE)
_MILESTONE_ID_RE = re.compile(r"^### (M-\d+) —")

# Scope-reduction warning vocabulary (deferral/degradation signals), kept distinct
# from the hard placeholder terms (TODO/TBD/???) so nothing is both a hard failure
# and a soft warning, and tuned to avoid legitimate engineering language.
_SCOPE_REDUCTION_TERMS = ("v1", "simplified", "for now", "stub")

# Notes are append-only `- Note: <date> [<Label>] <text>` lines inside a task
# entry (task-edit-and-task-note REQ-06). The label is bracketed data in the
# value, never the field key, so the plan parser keeps one key for every note.
# `Edit` is minted only by a forced `task edit`, never by `--label`.
NOTE_LABELS = ("Note", "Design", "Resolution", "Descoped", "Edit")
NOTE_DEFAULT_LABEL = "Note"
NOTE_FORCED_LABEL = "Edit"
NOTE_FIELD = "Note"
_NOTE_VALUE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \[([^\[\]]+)\] (\S.*)$")


def note_text(text: str) -> str:
    """Collapse *text* to a single line, or raise if it holds no content."""
    collapsed = " ".join((text or "").split())
    if not collapsed:
        raise SpecfloError("Note text is empty: pass some text to record.")
    return collapsed


def note_label(label: str | None, *, allow_edit: bool = False) -> str:
    """Return the validated label for a note, defaulting to ``Note``."""
    if label is None:
        return NOTE_DEFAULT_LABEL
    offered = [lb for lb in NOTE_LABELS if lb != NOTE_FORCED_LABEL]
    if label == NOTE_FORCED_LABEL and not allow_edit:
        raise SpecfloError(
            f"Label {NOTE_FORCED_LABEL!r} is reserved for `task edit --force`. "
            f"Accepted labels: {', '.join(offered)}."
        )
    if label not in NOTE_LABELS:
        raise SpecfloError(
            f"Unknown note label {label!r}. Accepted labels: {', '.join(offered)}."
        )
    return label


def format_note(
    text: str, label: str | None = None, today: str | None = None,
    *, allow_edit: bool = False,
) -> str:
    """Render the value of a ``- Note:`` line: ``<date> [<Label>] <text>``."""
    resolved = note_label(label, allow_edit=allow_edit)
    body = note_text(text)
    return f"{today or datetime.date.today().isoformat()} [{resolved}] {body}"


def parse_note(value: str) -> dict | None:
    """Split a note value back into ``{date, label, text}``; None if malformed.

    A hand-written note that does not parse is a plan warning, never a hard
    validation failure (REQ-10), so this returns None rather than raising.
    """
    m = _NOTE_VALUE_RE.match((value or "").strip())
    if not m:
        return None
    date, label, text = m.groups()
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        return None
    if label not in NOTE_LABELS:
        return None
    return {"date": date, "label": label, "text": text.strip()}


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
    needs: list[str] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    notes_malformed: list[str] = field(default_factory=list)

    @property
    def file_list(self) -> list[str]:
        """The Files field as a normalized path list (fan-out-plans REQ-04)."""
        return parse_files(self.files)


def parse_files(value: str | None) -> list[str]:
    """Parse a task's Files text into a list of paths (fan-out-plans REQ-04).

    Split on commas, trim whitespace, drop one trailing parenthetical note per
    entry (``~/x/y (venv, outside repo)`` -> ``~/x/y``), and drop empty
    entries. Because a note may itself contain commas, entries are split on
    commas outside parentheses. The plan.md text is never rewritten.
    """
    if not value:
        return []
    entries, depth, current = [], 0, []
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        if ch == "," and depth == 0:
            entries.append("".join(current)); current = []
        else:
            current.append(ch)
    entries.append("".join(current))
    out = []
    for entry in entries:
        entry = entry.strip()
        if entry.endswith(")"):
            entry = re.sub(r"\s*\([^()]*\)$", "", entry).strip()
        if entry:
            out.append(entry)
    return out


def parse_needs(value: str | None) -> list[str]:
    """Parse a task's Needs text into pool names (fan-out-plans REQ-07)."""
    return _split_refs(value or "")


def validate_pool_name(name: str) -> str:
    """Return *name* if it is a valid pool name: a non-empty token without
    commas or whitespace (fan-out-plans REQ-07); else raise SpecfloError."""
    if not name or "," in name or any(ch.isspace() for ch in name):
        raise SpecfloError(
            f"Invalid pool name {name!r}: must be a non-empty token without "
            "commas or whitespace."
        )
    return name


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
        notes: list[dict] = []
        notes_malformed: list[str] = []
        for ln in lines[start + 1:end]:
            if ln.startswith("- ") and ":" in ln:
                key, _, val = ln[2:].partition(":")
                key = key.strip()
                if key == NOTE_FIELD:
                    # Notes repeat: collect them in document order instead of
                    # letting the last one win the single-value field dict.
                    note = parse_note(val.strip())
                    if note is None:
                        # A hand-written note that does not parse is a plan
                        # warning, never a hard failure (REQ-10): keep the raw
                        # text so the warning can quote it.
                        notes_malformed.append(val.strip())
                    else:
                        notes.append(note)
                    continue
                fields[key] = val.strip()
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
            needs=parse_needs(fields.get("Needs")),
            milestone=fields.get("Milestone"),
            notes=notes,
            notes_malformed=notes_malformed,
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
        spec_doc = sp.read_text()
        active_reqs = spec_mod.active_requirement_ids(spec_doc)
        smap = spec_mod.supersession_map(spec_doc)
        covered: set[str] = set()
        for t in active:
            if not t.implements:
                issues.append(f"{t.id} implements no requirement (needs Implements: REQ-NN).")
            for req in t.implements:
                resolved = spec_mod.resolve_requirement(req, active_reqs, smap)
                if resolved is None:
                    issues.append(f"{t.id} implements {req}, which is not an active requirement.")
                else:
                    covered.add(resolved)
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
            # `_parse_tasks` keeps only the last of repeated fields, so a
            # hand-edited task with two `- Milestone:` lines would parse as
            # single-membership; catch the raw duplicate here (REQ-03).
            if markdown.count_entry_field(doc, t.id, "Milestone") > 1:
                issues.append(
                    f"{t.id} carries more than one Milestone field — a task must "
                    f"cite exactly one milestone."
                )
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
            milestone_reqs = {
                resolved
                for t in active if t.milestone in order
                for r in t.implements
                if (resolved := spec_mod.resolve_requirement(r, active_reqs, smap)) is not None
            }
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
    tasks = _parse_tasks(path.read_text())
    active = [t for t in tasks if t.status == "active"]
    # A superseded entry is frozen, but an unreadable note in it is still lost
    # history worth reporting (review-2 F6).
    for t in tasks:
        for raw in t.notes_malformed:
            warnings.append(
                f'{t.id} has a malformed note ("{raw}") — expected '
                '"<YYYY-MM-DD> [Label] text"; it is not read back.'
            )
    for t in active:
        haystack = f"{t.text} {t.acceptance} {t.verify}".lower()
        for term in _SCOPE_REDUCTION_TERMS:
            if re.search(rf"\b{re.escape(term)}\b", haystack):
                warnings.append(
                    f'{t.id} may reduce scope ("{term}") — deliver what the requirement needs, or split.'
                )
    warnings.extend(_shared_file_warnings(active))
    return warnings


def _shared_file_warnings(active: list[Task]) -> list[str]:
    """One non-blocking warning per pair of active tasks that share a normalized
    path and have no dependency path between them in either direction
    (fan-out-plans REQ-05). Pairs joined by a direct or transitive Depends on
    edge are ordered already, so they are silent."""
    ids = {t.id for t in active}
    deps = {t.id: [d for d in t.depends_on if d in ids] for t in active}

    def ancestors(tid: str) -> set[str]:
        seen, stack = set(), list(deps.get(tid, []))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(deps.get(cur, []))
        return seen

    reach = {t.id: ancestors(t.id) for t in active}
    warnings: list[str] = []
    for i, a in enumerate(active):
        if not a.file_list:
            continue
        for b in active[i + 1:]:
            shared = [f for f in a.file_list if f in b.file_list]
            if not shared or a.id in reach[b.id] or b.id in reach[a.id]:
                continue
            warnings.append(
                f"{a.id} and {b.id} share {', '.join(shared)} with no dependency "
                "between them — order them (Depends on) or split the file."
            )
    return warnings


def resolution_notes(root: Path, cfg: SpecfloConfig, slug: str) -> list[str]:
    """One non-blocking line per citation resolved through the supersession
    chain ('T-NN covers REQ-B via superseded REQ-A'). Read-only; empty when
    every citation is active (dead ends are blocking issues, not notes)."""
    path = plan_path(root, cfg, slug)
    sp = spec_mod.spec_path(root, cfg, slug)
    if not path.is_file() or not sp.is_file():
        return []
    spec_doc = sp.read_text()
    active_reqs = spec_mod.active_requirement_ids(spec_doc)
    smap = spec_mod.supersession_map(spec_doc)
    notes: list[str] = []
    for t in _parse_tasks(path.read_text()):
        if t.status != "active":
            continue
        for req in t.implements:
            if req in active_reqs:
                continue
            resolved = spec_mod.resolve_requirement(req, active_reqs, smap)
            if resolved is not None:
                notes.append(f"{t.id} covers {resolved} via superseded {req}")
    return notes


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


def check_implements(
    root: Path, cfg: SpecfloConfig, slug: str, implements: list[str]
) -> None:
    """Raise SpecfloError unless every id in *implements* is an active requirement.

    Shared by :func:`add_task` and :func:`edit_task` so a task cannot be edited
    into citing a requirement `task add` would have refused.
    """
    sp = spec_mod.spec_path(root, cfg, slug)
    if not sp.is_file():
        raise SpecfloError("Cannot link requirements: no spec.md for this project.")
    spec_doc = sp.read_text()
    active_reqs = spec_mod.active_requirement_ids(spec_doc)
    smap = spec_mod.supersession_map(spec_doc)
    for req_id in implements:
        if req_id in active_reqs:
            continue
        resolved = spec_mod.resolve_requirement(req_id, active_reqs, smap)
        if resolved is not None:
            raise SpecfloError(
                f"Cannot implement {req_id}: superseded by {resolved}; "
                f"cite {resolved} instead."
            )
        raise SpecfloError(
            f"Cannot implement {req_id}: not an active requirement in spec.md."
        )


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
    needs: list[str] | None = None,
    today: str | None = None,
) -> Task:
    """Append a task to the Tasks section and return it.

    Mints the next ``T-NN``. ``acceptance``/``verify`` are mandatory; ``implements``
    must name ≥1 active requirement in ``spec.md``. ``depends_on`` and
    ``supersedes`` must reference existing tasks. ``milestone``, when given, must
    name a milestone present in ``## Milestones`` and is written as the task's
    single ``- Milestone:`` field.
    """
    needs = [validate_pool_name(n) for n in (needs or [])]
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    with locked(lock_path_for(root, slug, path)):
        doc = path.read_text()
        if "## Tasks" not in doc:
            raise SpecfloError("Malformed plan.md: no '## Tasks' section.")

        depends_on = depends_on or []
        if not implements:
            raise SpecfloError("A task must implement at least one requirement (--from REQ-NN).")

        check_implements(root, cfg, slug, implements)

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
        if needs:
            entry_lines.append(f"- Needs: {', '.join(needs)}")
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
        milestone=milestone, needs=needs,
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
    with locked(lock_path_for(root, slug, path)):
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
    with locked(lock_path_for(root, slug, path)):
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


# The fields `task edit` rewrites in place (REQ-01). `title` lives in the entry
# heading; the rest are single `- <Field>:` lines. Depends on is edited by the
# add/drop flags instead, because an edge needs existence and cycle checks.
EDITABLE_FIELDS = ("title", "acceptance", "verify", "scope", "files", "needs",
                   "implements")
_EDIT_FIELD_KEYS = {
    "acceptance": "Acceptance", "verify": "Verify", "scope": "Scope",
    "files": "Files", "needs": "Needs", "implements": "Implements",
}


# The fields `task add` will not create empty: blanking one would leave a task
# nobody can act on, so `task edit` refuses it (review-1 F2). The rest are
# optional, and an empty value clears the line.
_MANDATORY_EDIT_FIELDS = ("title", "acceptance", "verify", "implements")


def _normalize_edit_value(name: str, value: str) -> str:
    """The value as it will be written: the same text the parser reads back.

    Normalizing once - before the checks and before the write - keeps
    :func:`_edit_is_a_change` and :func:`_apply_edit` from disagreeing about what
    a padded or comma-only value means (review-2 F2, F5).
    """
    if name in ("needs", "implements"):
        return ", ".join(_split_refs(value))
    return value.strip()


def _check_edit_value(name: str, value: str) -> None:
    """Reject a normalized edit value that a task field cannot hold."""
    # plan.md is read back with str.splitlines(), which breaks on far more than
    # \n and \r - a bare \x1e or \u2028 would inject a metadata line (review-2 F1).
    if value and value.splitlines() != [value]:
        raise SpecfloError(
            f"A task's {name} is one line: remove the line break "
            f"(record longer prose with `specflo task note`)."
        )
    if not value and name in _MANDATORY_EDIT_FIELDS:
        raise SpecfloError(
            f"A task's {name} cannot be empty. Supersede the task instead if it "
            f"no longer describes real work."
        )


def _edit_is_a_change(task: Task, name: str, value: str) -> bool:
    """True when writing *value* into *name* would change the task's parsed state."""
    if name == "title":
        return value != task.text
    if name == "needs":
        return _split_refs(value) != task.needs
    if name == "implements":
        return _split_refs(value) != task.implements
    return value != (getattr(task, name) or "")


def _apply_edit(doc: str, task_id: str, name: str, value: str) -> str:
    """Write an already-normalized *value* into the task's field."""
    if name == "title":
        # A replacement *function*: nothing in the user's title is interpreted as
        # an escape or a group reference (review-1 F1).
        return re.sub(
            rf"(?m)^### {re.escape(task_id)} —.*$",
            lambda _m: f"### {task_id} — {value}", doc, count=1,
        )
    return _write_or_clear(doc, task_id, _EDIT_FIELD_KEYS[name], value)


def _write_or_clear(doc: str, item_id: str, field: str, value: str) -> str:
    """Set *field* to *value*, or drop the line entirely when *value* is empty."""
    if not value:
        return markdown.clear_entry_field(doc, item_id, field)
    return markdown.set_entry_field(doc, item_id, field, value)


_EDIT_FIELD_LABELS = {
    "title": "Title", "acceptance": "Acceptance", "verify": "Verify",
    "scope": "Scope", "files": "Files", "needs": "Needs",
    "implements": "Implements", "depends_on": "Depends on",
}


def _forced_edit_note(task: Task, name: str) -> str:
    """The text of the ``[Edit]`` note a forced edit of *name* leaves behind."""
    if name == "title":
        old = task.text
    elif name in ("needs", "implements", "depends_on"):
        old = ", ".join(getattr(task, name))
    else:
        old = getattr(task, name) or ""
    return f'forced edit of {_EDIT_FIELD_LABELS[name]}; previous value: "{old}"'


def _check_edit_gate(task: Task, force: bool) -> None:
    """Refuse to edit finished work, so supersession stays the way it changes (REQ-03).

    A superseded entry is frozen outright; a done task opens only to ``force``,
    which records an ``[Edit]`` note of what it overwrote.
    """
    if task.status != "active":
        raise SpecfloError(
            f"Task {task.id} is superseded; its entry is frozen. Edit the task "
            f"that superseded it instead."
        )
    if task.progress == "done" and not force:
        raise SpecfloError(
            f"Task {task.id} is done: completed work changes by supersession, not "
            f"by editing. Record the reasoning with `specflo task note {task.id}`, "
            f"replace it with `specflo task add --supersedes {task.id}`, or pass "
            f"--force to edit it anyway (a note recording the old value is added)."
        )


def _edited_depends_on(
    tasks: list[Task], task: Task, add: list[str], drop: list[str]
) -> list[str]:
    """The task's Depends on list after *add* and *drop*, or raise.

    Every edge is checked before anything is written: an unknown task, an edge to
    the task itself, dropping an edge the task does not have, and an edge that
    would close a cycle are all refusals (REQ-02). Re-adding an existing edge is
    a no-op, not a duplicate.
    """
    known = {t.id for t in tasks}
    for dep in add + drop:
        if dep not in known:
            raise SpecfloError(f"No task {dep}.")
    for dep in add:
        if dep == task.id:
            raise SpecfloError(f"{task.id} cannot depend on itself.")
    deps = list(task.depends_on)
    for dep in drop:
        if dep not in deps:
            raise SpecfloError(f"{task.id} does not depend on {dep}.")
        deps.remove(dep)
    for dep in add:
        if dep not in deps:
            deps.append(dep)
    proposed = [replace(t, depends_on=deps) if t.id == task.id else t for t in tasks]
    cycle = _find_cycle([t for t in proposed if t.status == "active"])
    if cycle:
        raise SpecfloError(
            f"Editing {task.id} would create a dependency cycle: " + " -> ".join(cycle) + "."
        )
    return deps


def edit_task(
    root: Path, cfg: SpecfloConfig, slug: str, task_id: str,
    title: str | None = None, acceptance: str | None = None,
    verify: str | None = None, scope: str | None = None, files: str | None = None,
    needs: str | None = None, implements: str | None = None,
    add_depends_on: list[str] | None = None,
    drop_depends_on: list[str] | None = None,
    force: bool = False, today: str | None = None,
) -> tuple[str, list[str]]:
    """Rewrite a task's single-line fields in place; return ``(id, changed)``.

    Only the named field lines and the frontmatter date are touched, and a field
    already carrying the requested value is reported as unchanged rather than
    rewritten. Everything is validated before the write, so a refusal leaves
    plan.md byte-identical.
    """
    edits = {
        name: _normalize_edit_value(name, value) for name, value in (
            ("title", title), ("acceptance", acceptance), ("verify", verify),
            ("scope", scope), ("files", files), ("needs", needs),
            ("implements", implements),
        ) if value is not None
    }
    add_deps = list(add_depends_on or [])
    drop_deps = list(drop_depends_on or [])
    if not edits and not add_deps and not drop_deps:
        flags = [f"--{name.replace('_', '-')}" for name in EDITABLE_FIELDS]
        raise SpecfloError(
            "Nothing to edit: pass at least one of " + ", ".join(flags)
            + ", --add-depends-on, --drop-depends-on."
        )
    for name, value in edits.items():
        _check_edit_value(name, value)
    if implements is not None:
        check_implements(root, cfg, slug, _split_refs(edits["implements"]))
    if needs is not None:
        for pool in _split_refs(edits["needs"]):
            validate_pool_name(pool)
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    with locked(lock_path_for(root, slug, path)):
        doc = path.read_text()
        tasks = _parse_tasks(doc)
        task = next((t for t in tasks if t.id == task_id), None)
        if task is None:
            raise SpecfloError(f"No task {task_id}.")
        _check_edit_gate(task, force)
        changed = [
            name for name in EDITABLE_FIELDS
            if name in edits and _edit_is_a_change(task, name, edits[name])
        ]
        deps = _edited_depends_on(tasks, task, add_deps, drop_deps)
        if deps != task.depends_on:
            changed.append("depends_on")
        if changed:
            for name in changed:
                if name == "depends_on":
                    doc = _write_or_clear(doc, task_id, "Depends on", ", ".join(deps))
                else:
                    doc = _apply_edit(doc, task_id, name, edits[name])
                # A forced edit of finished work records what it overwrote, in
                # the same locked write as the edit itself (REQ-04).
                if force and task.progress == "done":
                    doc = markdown.append_entry_field(
                        doc, task_id, NOTE_FIELD,
                        format_note(_forced_edit_note(task, name), NOTE_FORCED_LABEL,
                                    today, allow_edit=True),
                    )
            doc = markdown.bump_updated(doc, today)
            path.write_text(doc)
    return task_id, changed


def add_note(
    root: Path, cfg: SpecfloConfig, slug: str, task_id: str, text: str,
    label: str | None = None, today: str | None = None,
    *, allow_edit: bool = False,
) -> dict:
    """Append one ``- Note:`` line to a task entry and return the note record.

    Notes are append-only history, so this works on a task in any progress state
    and on a superseded one (REQ-05). The label and text are validated before the
    lock is taken, so a rejected note leaves plan.md byte-identical.
    """
    value = format_note(text, label, today, allow_edit=allow_edit)
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    with locked(lock_path_for(root, slug, path)):
        doc = path.read_text()
        if not any(t.id == task_id for t in _parse_tasks(doc)):
            raise SpecfloError(f"No task {task_id}.")
        doc = markdown.append_entry_field(doc, task_id, NOTE_FIELD, value)
        doc = markdown.bump_updated(doc, today)
        path.write_text(doc)
    return parse_note(value)


def _set_progress(
    root: Path, cfg: SpecfloConfig, slug: str, task_id: str,
    progress: str, reason: str | None = None, note: str | None = None,
    today: str | None = None,
) -> Task:
    if progress not in PROGRESS_STATES:
        raise SpecfloError(f"Unknown progress state {progress!r}.")
    # The ride-along note is validated before the lock, so a bad note refuses the
    # transition outright instead of leaving a half-applied write (REQ-11).
    note_value = format_note(note, today=today) if note is not None else None
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    with locked(lock_path_for(root, slug, path)):
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
        if note_value is not None:
            doc = markdown.append_entry_field(doc, task_id, NOTE_FIELD, note_value)
        doc = markdown.bump_updated(doc, today)
        path.write_text(doc)
    task.progress = progress
    task.blocked = reason if progress == "blocked" else None
    return task


def start_task(root, cfg, slug, task_id, today=None) -> Task:
    return _set_progress(root, cfg, slug, task_id, "in_progress", today=today)


def done_task(root, cfg, slug, task_id, note=None, today=None) -> Task:
    return _set_progress(root, cfg, slug, task_id, "done", note=note, today=today)


def block_task(root, cfg, slug, task_id, reason=None, today=None) -> Task:
    return _set_progress(root, cfg, slug, task_id, "blocked", reason=reason, today=today)


def reopen_task(root, cfg, slug, task_id, note=None, today=None) -> Task:
    return _set_progress(root, cfg, slug, task_id, "pending", note=note, today=today)


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
    with locked(lock_path_for(root, slug, path)):
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


def first_in_progress(active: list[Task]) -> str | None:
    """The first ``in_progress`` task id in *active*, or None.

    There is normally at most one task under way at a time; this is the task the
    project is *on* (:func:`current_task_id`). Shared so the reseed path and the
    progress-derived "next" view agree on which task that is.
    """
    return next((t.id for t in active if t.progress == "in_progress"), None)


POOLS_HEADER = "## Pools"
# Reserved pool: the task runs in the main session with the user. Needs no
# declaration and has one slot (fan-out-plans REQ-12).
USER_POOL = "user"
_POOL_LINE = re.compile(r"^- (?P<name>\S+): (?P<size>\d+)\s*$")


def _declared_pools(doc: str) -> dict[str, int]:
    """The pools declared in the CLI-owned ``## Pools`` section, in order."""
    body = markdown.section_body(doc, POOLS_HEADER) or ""
    pools: dict[str, int] = {}
    for line in body.splitlines():
        m = _POOL_LINE.match(line.strip())
        if m:
            pools[m.group("name")] = int(m.group("size"))
    return pools


def parse_pools(doc: str) -> dict[str, int]:
    """Merged pool map for *doc* (fan-out-plans REQ-08): every declared pool at
    its declared size, plus every pool an active task Needs, undeclared ones at
    size 1. Superseded tasks are ignored."""
    pools = _declared_pools(doc)
    for t in _parse_tasks(doc):
        if t.status != "active":
            continue
        for name in t.needs:
            pools.setdefault(name, 1)
    return pools


def list_pools(root: Path, cfg: SpecfloConfig, slug: str) -> dict[str, int]:
    """Read-only :func:`parse_pools` over the project's plan.md."""
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    return parse_pools(path.read_text())


def add_pool(
    root: Path, cfg: SpecfloConfig, slug: str, name: str, size: int,
    today: str | None = None,
) -> tuple[str, int]:
    """Declare (or resize) pool *name* with *size* slots (fan-out-plans REQ-08).

    Creates ``## Pools`` immediately before ``## Tasks`` on first use and writes
    or updates the pool's single ``- <name>: N`` line in place under the
    advisory lock. ``size`` must be >= 1.
    """
    name = validate_pool_name(name)
    if size < 1:
        raise SpecfloError(f"Pool size must be >= 1 (got {size}).")
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    with locked(lock_path_for(root, slug, path)):
        doc = path.read_text()
        if markdown.section_body(doc, "## Tasks") is None:
            raise SpecfloError("Malformed plan.md: no '## Tasks' section.")
        doc = markdown.ensure_section_before(doc, POOLS_HEADER, "## Tasks")
        line = f"- {name}: {size}"
        lines = doc.splitlines(keepends=True)
        start = next(i for i, ln in enumerate(lines) if ln.strip() == POOLS_HEADER)
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith("## ")), len(lines))
        for i in range(start + 1, end):
            m = _POOL_LINE.match(lines[i].strip())
            if m and m.group("name") == name:
                lines[i] = line + "\n"
                break
        else:
            # Append after the last pool line (or right after the header),
            # keeping the blank line that separates the section from ## Tasks.
            last = start
            for i in range(start + 1, end):
                if _POOL_LINE.match(lines[i].strip()):
                    last = i
            lines.insert(last + 1, line + "\n")
            if last == start and (last + 2 >= len(lines) or lines[last + 2].strip()):
                lines.insert(last + 2, "\n")
        doc = markdown.bump_updated("".join(lines), today)
        path.write_text(doc)
    return name, size


def _progress_from_tasks(
    active: list[Task], pools: dict[str, int] | None = None
) -> dict:
    by_state = {s: 0 for s in PROGRESS_STATES}
    for t in active:
        by_state[t.progress if t.progress in by_state else "pending"] += 1
    done_ids = {t.id for t in active if t.progress == "done"}
    # Files held by in-progress work: a dependency-ready pending task that
    # shares a normalized path with one is not ready until that task is done,
    # reopened or blocked (fan-out-plans REQ-06). Tasks with no Files are never
    # excluded, so a Files-free plan keeps today's ready set.
    held = {f for t in active if t.progress == "in_progress" for f in t.file_list}
    # Pool slots (fan-out-plans REQ-09): each in_progress task needing a pool
    # holds one slot; a pending needer is not ready while every slot of any
    # pool it needs is held. Undeclared pools (including 'user') have size 1.
    sizes = dict(pools or {})
    used: dict[str, int] = {}
    for t in active:
        if t.progress == "in_progress":
            for name in t.needs:
                used[name] = used.get(name, 0) + 1

    def pools_full(t: Task) -> bool:
        return any(used.get(name, 0) >= sizes.get(name, 1) for name in t.needs)

    next_actionable = [
        t.id for t in active
        if t.progress == "pending"
        and all(d in done_ids for d in t.depends_on)
        and not (held and held.intersection(t.file_list))
        and not pools_full(t)
    ]
    # When no unstarted task is dependency-ready but a task on the plan is already
    # under way (the mid-task state a context clear lands in, or a half-started
    # auto pass), that task *is* the work to continue — surface it rather than read
    # the project as stuck. This keeps the "steps past a task already under way"
    # behaviour for as long as other pending work is ready; only once the
    # in-progress task is the sole remaining work does it become the "next" task,
    # so status / `task show` / checkpoint agree with `current_task_id` instead of
    # declaring nothing actionable.
    if not next_actionable:
        cont = first_in_progress(active)
        if cont is not None:
            next_actionable = [cont]
    total = len(active)
    return {
        "total": total,
        "by_state": by_state,
        "done": by_state["done"],
        "next_actionable": next_actionable,
        "all_done": total > 0 and by_state["done"] == total,
    }


def progress_from_doc(doc: str) -> dict:
    active = [t for t in _parse_tasks(doc) if t.status == "active"]
    prog = _progress_from_tasks(active, parse_pools(doc))
    # Steer the ready-task ordering to the current milestone so status's "next"
    # line, the next-step hint, and checkpoint's next-task note all lead with the
    # same task `task show` picks (REQ-13); dormant on milestone-free plans (REQ-04).
    prog["next_actionable"] = _steer_actionable(
        prog["next_actionable"], active, _parse_milestones(doc))
    return prog


def execution_graph(root: Path, cfg: SpecfloConfig, slug: str) -> dict:
    """The active tasks and milestones of the plan for :mod:`specflo.graph`
    (fan-out-plans REQ-13). Read-only; raises the no-plan error when there is
    no plan.md."""
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        raise SpecfloError("No plan yet. Run `specflo plan start` first.")
    doc = path.read_text()
    return {
        "tasks": [t for t in _parse_tasks(doc) if t.status == "active"],
        "milestones": _parse_milestones(doc),
    }


def frontier(root: Path, cfg: SpecfloConfig, slug: str) -> dict:
    """The orchestrator's frontier (fan-out-plans REQ-10), read-only.

    ``tasks`` carries, per active task, its ``files`` and ``needs`` lists and
    ``ready``: True exactly for the *pending* ids in ``next_actionable``. When
    every pending task is held back, ``next_actionable`` falls back to the
    in-progress task (the linear continue-in-progress behaviour); that task is
    never ``ready``, so an orchestrator sees an empty frontier and waits rather
    than dispatching a second agent onto it (review round 1, F1). ``pools``
    maps each declared or needed pool to ``{size, holders}`` where holders are
    the in_progress tasks naming it.
    """
    path = plan_path(root, cfg, slug)
    doc = path.read_text() if path.is_file() else ""
    active = [t for t in _parse_tasks(doc) if t.status == "active"]
    nexts = set(progress_from_doc(doc)["next_actionable"])
    ready = {t.id for t in active if t.progress == "pending" and t.id in nexts}
    pools = {
        name: {
            "size": size,
            "holders": [t.id for t in active
                        if t.progress == "in_progress" and name in t.needs],
        }
        for name, size in parse_pools(doc).items()
    }
    return {
        "tasks": [
            {"id": t.id, "files": t.file_list, "needs": t.needs, "ready": t.id in ready}
            for t in active
        ],
        "pools": pools,
    }


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
    """The earliest-in-document-order incomplete milestone id that has member
    tasks, or None if all are complete (or there are no milestones). An empty
    milestone (no members) carries no work, so it is skipped rather than treated
    as the current milestone (validate flags it separately, REQ-09)."""
    for r in rollups:
        if not r["complete"] and r["total"] > 0:
            return r["id"]
    return None


def _steer_actionable(
    actionable: list[str], active: list[Task], milestones: list[Milestone]
) -> list[str]:
    """Order dependency-ready task ids so the current milestone's tasks lead, then
    later milestones in document order (unassigned tasks last), stably preserving
    document order within each group (REQ-13). Dormant — returns *actionable*
    unchanged — with no milestones or once all are complete (REQ-04 dormancy).

    Shared by :func:`_default_actionable` (which takes the head) and
    :func:`progress_from_doc` (so status, the next-step hint, and checkpoint lead
    with the same task ``task show`` picks).
    """
    if not actionable or not milestones:
        return actionable
    rollups = _milestone_rollups(milestones, active)
    current = _current_milestone(rollups)
    if current is None:
        return actionable
    order = {r["id"]: i for i, r in enumerate(rollups)}
    by_id = {t.id: t for t in active}

    def _key(tid: str) -> tuple[int, int]:
        ms = by_id[tid].milestone
        return (0, 0) if ms == current else (1, order.get(ms, len(order)))

    return sorted(actionable, key=_key)


def _default_actionable(
    active: list[Task], milestones: list[Milestone],
    pools: dict[str, int] | None = None,
) -> str | None:
    """The task ``task show`` defaults to: among dependency-ready pending tasks,
    steer to the current milestone (REQ-13).

    The candidate set is exactly ``next_actionable`` — every dependency-ready
    pending task — so milestones never make a ready task *unselectable*; they only
    pick *which* ready task is the default (:func:`_steer_actionable`). A ready
    task in the current milestone wins; if none is ready there, the
    earliest-milestone ready task is offered (and flagged working-ahead
    separately). With no milestones — or all complete — this is today's
    ``next_actionable[0]`` (REQ-04 dormancy).
    """
    steered = _steer_actionable(
        _progress_from_tasks(active, pools)["next_actionable"], active, milestones)
    return steered[0] if steered else None


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

    def _beat(r: dict | None, all_complete: bool) -> dict | None:
        return None if r is None else {
            "id": r["id"], "title": r["title"],
            "exit_items": r["exit_items"], "all_complete": all_complete,
        }

    if current is None:
        # Every milestone is complete (or empty): surface the last *complete* one.
        return _beat(next((r for r in reversed(rollups) if r["complete"]), None), True)
    # We sit *at* the boundary only while no task of the current milestone has
    # started; the moment one is in progress or done we have moved past it.
    if any(
        t.milestone == current and t.progress in ("in_progress", "done")
        for t in active
    ):
        return None
    # The just-completed milestone is the nearest *complete* one before the
    # current milestone — nearest-complete, not idx-1, so an empty earlier
    # milestone (invalid plan) never masquerades as just-completed (REQ-14).
    idx = next(i for i, r in enumerate(rollups) if r["id"] == current)
    return _beat(next((r for r in reversed(rollups[:idx]) if r["complete"]), None), False)


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


def current_task_id(root: Path, cfg: SpecfloConfig, slug: str) -> str | None:
    """The task the project is *on*: the first in_progress one, else the default.

    ``next_actionable`` lists dependency-ready **pending** tasks, so it steps past
    a task already under way. Anything resuming mid-execute — a reseed after a
    context clear, most of all — wants the task being worked, not the one after
    it. Falls back to :func:`_default_actionable` when nothing is in_progress, and
    returns ``None`` when there is no plan or no candidate.
    """
    path = plan_path(root, cfg, slug)
    if not path.is_file():
        return None
    doc = path.read_text()
    active = [t for t in _parse_tasks(doc) if t.status == "active"]
    started = first_in_progress(active)
    return started or _default_actionable(active, _parse_milestones(doc), parse_pools(doc))


def render_task_brief(brief: dict) -> str:
    """Render a :func:`task_brief` payload as the text block ``task show`` prints.

    Header, acceptance, verify, the cited REQ-NN sections and the plan's Global
    constraints — everything the brief carries *except* the soft milestone
    boundary beat, which is a user-facing prompt its callers append themselves.
    One renderer, so the reseed payload's inlined brief (pi-extension REQ-19) and
    ``specflo task show`` cannot drift.

    Returns ``""`` when the brief has no task (every task done).
    """
    t = brief["task"]
    if t is None:
        return ""
    header = f"{t['id']} - {t['text']}  [{t['progress']}]"
    if brief.get("working_ahead"):
        header += "  — working ahead (later milestone than current)"
    execution_line = f"Execution: {brief['execution']}"
    resolutions = {
        r["id"]: r["resolved"] for r in brief["requirements"] if r.get("resolved")
    }
    implements = [
        f"{req} -> superseded by {resolutions[req]}" if req in resolutions else req
        for req in t["implements"]
    ]
    lines = [
        header,
        execution_line,
        f"  Acceptance: {t['acceptance']}",
        f"  Verify:     {t['verify']}",
        f"  Implements: {', '.join(implements)}",
    ]
    if t["depends_on"]:
        lines.append(f"  Depends on: {', '.join(t['depends_on'])}")
    # Ownership and resources (fan-out-plans REQ-11): each line only when the
    # field holds something, so a Files-free plan renders exactly as before.
    if t.get("files"):
        lines.append(f"  Files:      {', '.join(t['files'])}")
    if t.get("needs"):
        lines.append(f"  Needs:      {', '.join(t['needs'])}")
        if USER_POOL in t["needs"]:
            lines.append(
                f"  This task needs the '{USER_POOL}' pool: it is not delegated "
                "and runs with the user in the main session."
            )
    # Notes (task-edit-and-task-note REQ-08): the task's own history, printed
    # only when it has some, so a note-free brief renders exactly as before.
    if t.get("notes"):
        lines.append("  Notes:")
        for note in t["notes"]:
            lines.append(f"    {note['date']} [{note['label']}] {note['text']}")
    lines.append("")
    for req in brief["requirements"]:
        lines.append(req["section"].rstrip() if req["section"]
                     else f"### {req['id']} - (not found in spec)")
        lines.append("")
    if brief["global_constraints"]:
        lines.append("## Global constraints")
        lines.append(brief["global_constraints"])
    # Keep the rendered brief ASCII for terminal/hook output (the em-dash -> ASCII
    # cleanup the CLI output guard locks): em-dashes in authored section headings
    # become hyphens. The structured `brief` dict still carries the verbatim
    # sections for `-json` consumers / `task show <id> --json`.
    return "\n".join(lines).replace("—", "-")


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
    constraints = markdown.strip_comments(
        markdown.section_body(doc, "## Global constraints") or ""
    ).strip() or None
    # The recorded execution mode (fan-out-plans REQ-03). Under fan-out the
    # working-ahead note is dropped: lanes crossing milestones is the expected
    # shape, not a warning (REQ-14).
    execution = load_project(root, cfg, slug).execution
    fan_out = execution == FAN_OUT_EXECUTION
    if task_id is None:
        task_id = _default_actionable(active, milestones, parse_pools(doc))
        if task_id is None:
            boundary = milestone_boundary_from_doc(doc)
            if boundary is not None and boundary.get("all_complete"):
                # Every task is done: there is no task to brief, but the execute
                # surface must still surface the final milestone's Exit checklist
                # as the all-complete boundary beat rather than error out (REQ-14).
                return {
                    "task": None, "requirements": [],
                    "global_constraints": constraints,
                    "execution": execution,
                    "working_ahead": None, "boundary": boundary,
                }
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
    active_reqs = spec_mod.active_requirement_ids(spec_doc)
    smap = spec_mod.supersession_map(spec_doc)
    requirements = []
    for req in task.implements:
        resolved = spec_mod.resolve_requirement(req, active_reqs, smap)
        if resolved is not None and resolved != req:
            # superseded citation: present the ultimate active superseder's
            # section as the live requirement, never the stale one
            requirements.append({
                "id": req, "resolved": resolved,
                "section": spec_mod.requirement_section(spec_doc, resolved),
            })
        else:
            requirements.append(
                {"id": req, "section": spec_mod.requirement_section(spec_doc, req)}
            )
    return {
        "task": {
            "id": task.id, "text": task.text, "acceptance": task.acceptance,
            "verify": task.verify, "implements": task.implements,
            "depends_on": task.depends_on, "files": task.file_list,
            "needs": task.needs,
            "scope": task.scope, "progress": task.progress,
            "notes": task.notes,
        },
        "requirements": requirements,
        "global_constraints": constraints,
        "execution": execution,
        "working_ahead": (
            None if fan_out else _task_working_ahead(task, milestones, active)
        ),
        # The soft milestone-boundary verify beat (None off a boundary), so the
        # execute surface can surface the just-completed milestone's Exit checklist
        # alongside the next task (REQ-14).
        "boundary": milestone_boundary_from_doc(doc),
    }
