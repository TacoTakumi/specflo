"""The brief: the one document a quick-level project is worked from.

A quick project has no brainstorm, spec or plan. Its ``brief.md`` holds the
goal, the one check that says the work is done, the proof that the check
passes, and any work found along the way that the brief does not do.

A harden-level project has a brief too, with other sections: the scope its
review rounds harden, what they focus on, and when hardening stops. Its plan
starts empty and grows from review findings as fix tasks.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from . import markdown
from .config import SpecfloConfig
from .projects import HARDEN_LEVEL, load_project, project_dir

BRIEF_FILENAME = "brief.md"

# The brief's sections, in document order.
SECTIONS = ("Goal", "Done when", "Proof", "Deferred")

_TEMPLATE = """\
---
project: {slug}
level: quick
status: draft
created: {today}
updated: {today}
---

# Brief: {name}

## Goal
<!-- one or two sentences: what this change does -->

## Done when
<!-- exactly one check, as a single list item -->

## Proof
<!-- test or command output that shows the check passes -->

## Deferred
<!-- work found along the way that this brief does not do (optional) -->
"""

# A harden brief's sections, in document order.
HARDEN_SECTIONS = ("Scope", "Focus", "Stop when")

_HARDEN_TEMPLATE = """\
---
project: {slug}
level: harden
status: draft
created: {today}
updated: {today}
---

# Brief: {name}

## Scope
<!-- the paths to harden, one list item each, or: the whole repo -->

## Focus
<!-- what the review rounds look at hardest, or: none -->

## Stop when
<!-- when hardening stops, beyond an empty ledger and every fix task done, or: none -->
"""


def brief_path(root: Path, cfg: SpecfloConfig, slug: str) -> Path:
    return project_dir(root, cfg, slug) / BRIEF_FILENAME


def start_brief(
    root: Path, cfg: SpecfloConfig, slug: str, today: str | None = None
) -> tuple[Path, bool]:
    """Create the brief, or locate an existing one; ``(path, created)``.

    Never clobbers: a brief that exists is returned as it is.
    """
    project = load_project(root, cfg, slug)
    path = brief_path(root, cfg, slug)
    if path.exists():
        return path, False
    today = today or datetime.date.today().isoformat()
    template = _HARDEN_TEMPLATE if project.level == HARDEN_LEVEL else _TEMPLATE
    path.write_text(template.format(slug=project.slug, name=project.name, today=today))
    return path, True


def _list_items(body: str) -> list[str]:
    """The top-level list items of a section body: ``-``, ``*`` or ``1.`` lines."""
    return [
        line for line in body.splitlines()
        if re.match(r"^(?:[-*]|\d+[.)])\s+\S", line)
    ]


def check_count(doc: str) -> int:
    """How many checks a brief's Done when section lists."""
    body = markdown.strip_comments(markdown.section_body(doc, "## Done when") or "")
    return len(_list_items(body))


def validate_brief(root: Path, cfg: SpecfloConfig, slug: str) -> list[str]:
    """The brief's gaps; empty when it has a goal, exactly one check and proof.

    Read-only. An empty Deferred section is not a gap: it is optional.
    """
    path = brief_path(root, cfg, slug)
    if not path.is_file():
        return ["brief.md not found - a quick project makes it at `specflo new`."]
    doc = path.read_text()
    issues = []
    bodies = {}
    for title in SECTIONS:
        body = markdown.section_body(doc, f"## {title}")
        if body is None:
            issues.append(f"missing '{title}' section.")
        bodies[title] = markdown.strip_comments(body or "").strip()
    if "Goal" in bodies and not bodies["Goal"]:
        issues.append("Goal is empty: say what this change does.")
    checks = _list_items(bodies.get("Done when", ""))
    if not checks:
        issues.append("Done when has no check: write exactly one, as a list item.")
    elif len(checks) > 1:
        issues.append(
            f"Done when has {len(checks)} checks; a quick brief has exactly one."
            " Keep one and move the rest to Deferred."
        )
    if "Proof" in bodies and not bodies["Proof"]:
        issues.append("Proof is empty: record the test or command output that shows the check passes.")
    return issues


def _entries(doc: str, prefix: str) -> dict[str, tuple[str, dict[str, str]]]:
    """Each ``### <prefix>NN — title`` entry: its title and its ``- Field:`` lines."""
    entries: dict[str, tuple[str, dict[str, str]]] = {}
    current = None
    for line in doc.splitlines():
        match = re.match(rf"^### ({re.escape(prefix)}\d+) — (.*)$", line)
        if match:
            current = match.group(1)
            entries[current] = (match.group(2).strip(), {})
        elif line.startswith("## "):
            current = None
        elif current and (field := re.match(r"^- ([A-Za-z ]+): (.*)$", line)):
            entries[current][1].setdefault(field.group(1), field.group(2).strip())
    return entries


def render_view(root: Path, cfg: SpecfloConfig, slug: str) -> str:
    """One read-only page for a fast project: goal, decisions, checks and tasks.

    Built from brainstorm.md, spec.md and plan.md each time; nothing is stored.
    Superseded entries are left out.
    """
    from . import brainstorm, plan, spec

    project = load_project(root, cfg, slug)
    base = project_dir(root, cfg, slug)

    def read(filename: str) -> str:
        path = base / filename
        return path.read_text() if path.is_file() else ""

    brainstorm_doc = read(brainstorm.BRAINSTORM_FILENAME)
    spec_doc = read(spec.SPEC_FILENAME)
    plan_doc = read(plan.PLAN_FILENAME)
    goal = markdown.strip_comments(markdown.section_body(spec_doc, "## Objective") or "").strip()

    lines = [f"# Brief: {project.name} ({project.level} level)", "", "## Goal", goal or "(none yet)", ""]
    lines.append("## Decisions")
    decisions = _entries(brainstorm_doc, "D-")
    for decision_id in brainstorm.active_decision_ids(brainstorm_doc):
        lines.append(f"- {decision_id} - {decisions[decision_id][0]}")
    lines += ["", "## Checks"]
    requirements = _entries(spec_doc, "REQ-")
    for req_id in spec.active_requirement_ids(spec_doc):
        title, fields = requirements[req_id]
        lines.append(f"- {req_id} - {title}")
        lines.append(f"  Acceptance: {fields.get('Acceptance', '')}")
    lines += ["", "## Tasks"]
    if plan_doc:
        for task in plan.list_tasks(root, cfg, slug):
            lines.append(f"- {task.id} - {task.text} [{task.progress}]")
    return "\n".join(lines) + "\n"


def seed_fast_documents(root: Path, cfg: SpecfloConfig, slug: str) -> None:
    """Seed the three fast-level documents from a quick project's brief.

    The goal and every Deferred item go into the brainstorm's Current
    understanding. Each Done when check becomes a requirement, in order, and
    one task implements the first, done when the brief has proof; any later
    check is named in the understanding as work still to plan. A brief with no
    check seeds no requirement and no task. The brief itself is left as it is,
    and a document that already exists is not seeded.
    """
    from . import brainstorm, plan, spec

    doc = brief_path(root, cfg, slug).read_text()

    def body(title: str) -> str:
        return markdown.strip_comments(markdown.section_body(doc, f"## {title}") or "").strip()

    goal = " ".join(body("Goal").split())
    items = _list_items(body("Done when"))
    checks = [" ".join(re.sub(r"^(?:[-*]|\d+[.)])\s+", "", item).split()) for item in items]
    deferred = _list_items(body("Deferred"))
    proof = body("Proof")

    bs_path, created = brainstorm.start_brainstorm(root, cfg, slug)
    if created:
        understanding = ["Carried from the quick brief." + (f" Goal: {goal}" if goal else "")]
        if checks[1:]:
            understanding += ["", "Checks beyond the first, now requirements with no task yet:"]
            understanding += [f"- {check}" for check in checks[1:]]
        if deferred:
            understanding += ["", "Deferred work to pick up at this level:", *deferred]
        text = bs_path.read_text()
        bs_path.write_text(markdown.replace_section_body(
            text, "## Current understanding", "\n".join(understanding) + "\n"
        ))
    requirement_ids = []
    if spec.start_spec(root, cfg, slug)[1]:
        seen = set()
        for check in checks:
            # add_requirement refuses a requirement the spec already holds.
            if check.casefold() in seen:
                continue
            seen.add(check.casefold())
            requirement = spec.add_requirement(root, cfg, slug, text=check, acceptance=check)
            requirement_ids.append(requirement.id)
    if plan.start_plan(root, cfg, slug)[1] and requirement_ids:
        task = plan.add_task(
            root, cfg, slug, text=goal or checks[0], acceptance=checks[0],
            verify="The brief's Proof section", implements=[requirement_ids[0]],
        )
        if proof:
            plan.start_task(root, cfg, slug, task.id)
            plan.done_task(root, cfg, slug, task.id, note="Proof carried from the quick brief.")
