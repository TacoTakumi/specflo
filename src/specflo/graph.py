"""The execution graph of a plan (fan-out-plans REQ-13).

Pure functions over the *active* ``Task`` and ``Milestone`` lists a caller has
already parsed from ``plan.md`` (superseded tasks are never passed in): waves by
longest dependency path, one edge per ``Depends on`` entry, a mermaid
``graph LR`` block, and the ``--json`` payload of ``specflo plan graph``.
Nothing here reads or writes a file.
"""

from __future__ import annotations

from .plan import Milestone, Task


def waves(tasks: list[Task]) -> list[list[str]]:
    """Group task ids by longest dependency path: wave 0 holds tasks with no
    dependencies (among *tasks*), wave N those whose deepest dependency sits in
    wave N-1. Dependencies on ids outside *tasks* are ignored. Ids keep their
    plan order within a wave."""
    ids = {t.id for t in tasks}
    deps = {t.id: [d for d in t.depends_on if d in ids] for t in tasks}
    depth: dict[str, int] = {}

    def _depth(tid: str, seen: tuple[str, ...] = ()) -> int:
        if tid in depth:
            return depth[tid]
        if tid in seen:  # a cycle: validate plan rejects it; don't recurse forever
            return 0
        d = deps[tid]
        depth[tid] = 0 if not d else 1 + max(_depth(x, seen + (tid,)) for x in d)
        return depth[tid]

    for t in tasks:
        _depth(t.id)
    out: list[list[str]] = []
    for t in tasks:
        level = depth[t.id]
        while len(out) <= level:
            out.append([])
        out[level].append(t.id)
    return out


def edges(tasks: list[Task]) -> list[list[str]]:
    """``[from, to]`` per ``Depends on`` entry, in plan order."""
    return [[dep, t.id] for t in tasks for dep in t.depends_on]


def _node(tid: str) -> str:
    return tid.replace("-", "")


def _label(text: str) -> str:
    # Mermaid labels sit inside double quotes; its own escape for a quote is
    # the `#quot;` entity.
    return text.replace('"', "#quot;")


def mermaid(tasks: list[Task], milestones: list[Milestone]) -> str:
    """A fenced mermaid ``graph LR`` block: one node per task labelled
    ``<id> <title>``, one subgraph per milestone holding its tasks (tasks with
    no milestone sit at the top level), and one edge line per dependency."""
    lines = ["```mermaid", "graph LR"]
    by_milestone: dict[str, list[Task]] = {m.id: [] for m in milestones}
    loose: list[Task] = []
    for t in tasks:
        if t.milestone in by_milestone:
            by_milestone[t.milestone].append(t)
        else:
            loose.append(t)
    for m in milestones:
        lines.append(f'  subgraph {_node(m.id)}["{_label(f"{m.id} {m.title}")}"]')
        for t in by_milestone[m.id]:
            lines.append(f'    {_node(t.id)}["{_label(f"{t.id} {t.text}")}"]')
        lines.append("  end")
    for t in loose:
        lines.append(f'  {_node(t.id)}["{_label(f"{t.id} {t.text}")}"]')
    for src, dst in edges(tasks):
        lines.append(f"  {_node(src)} --> {_node(dst)}")
    lines.append("```")
    return "\n".join(lines)


def payload(tasks: list[Task], milestones: list[Milestone]) -> dict:
    """The ``specflo plan graph --json`` payload: ``{waves, tasks, edges}``."""
    return {
        "waves": waves(tasks),
        "tasks": [
            {
                "id": t.id, "text": t.text, "progress": t.progress,
                "files": t.file_list, "needs": t.needs, "milestone": t.milestone,
            }
            for t in tasks
        ],
        "edges": edges(tasks),
    }
