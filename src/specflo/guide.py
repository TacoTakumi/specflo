"""The `guide` orientation payload — "what can I do here?" in one shot.

`build_guide` is zero-state safe: it takes an already-resolved ``root``/``cfg``
(either may be ``None`` for an uninitialized repo) and never mutates anything. It
reports what specflo is, the phase pipeline, the full command surface, and the
right "do this next" for whichever of the three repo states applies — so an agent
can run ``specflo guide`` cold and get up to speed before ``specflo init``.

The command table is curated here for human-friendly grouping/summaries; a
coverage guard test introspects the CLI and asserts every registered command
appears, so the table can't silently drift from the real surface.
"""

from __future__ import annotations

from pathlib import Path

from . import projects, workflow
from .config import SpecfloConfig
from .errors import SpecfloError

# Curated command table. ``name`` is the canonical command path (matched against
# the live CLI by the coverage guard); ``args`` is the metavar shown to humans.
COMMANDS: list[dict[str, str]] = [
    {"name": "init", "group": "setup", "args": "",
     "summary": "Scaffold .specflo/ and the projects dir."},
    {"name": "new", "group": "setup", "args": "<name>",
     "summary": "Create a project and make it active."},
    {"name": "list", "group": "setup", "args": "",
     "summary": "List all projects, marking the active one."},
    {"name": "switch", "group": "setup", "args": "<name>",
     "summary": "Make another project active."},
    {"name": "status", "group": "setup", "args": "",
     "summary": "Show the active project, its phase, and what's next."},
    {"name": "guide", "group": "setup", "args": "",
     "summary": "Show this overview of specflo and what to do next."},
    {"name": "checkpoint", "group": "setup", "args": "",
     "summary": "Print the resume prompt; refresh checkpoint.md."},
    {"name": "hook reseed", "group": "setup", "args": "",
     "summary": "Emit the session-start reseed payload (used by the SessionStart hook)."},
    {"name": "brainstorm start", "group": "workflow", "args": "",
     "summary": "Create the brainstorm.md artifact."},
    {"name": "decision add", "group": "workflow", "args": "",
     "summary": "Record a brainstorm decision (D-NN)."},
    {"name": "spec start", "group": "workflow", "args": "",
     "summary": "Create the spec.md artifact."},
    {"name": "requirement add", "group": "workflow", "args": "",
     "summary": "Record a spec requirement (REQ-NN)."},
    {"name": "validate", "group": "workflow", "args": "<artifact>",
     "summary": "Lint an artifact/phase (brainstorm|spec|plan|execute)."},
    {"name": "advance", "group": "workflow", "args": "",
     "summary": "Gate the current artifact, then move to the next phase."},
    {"name": "plan start", "group": "workflow", "args": "",
     "summary": "Create the plan.md artifact."},
    {"name": "task add", "group": "workflow", "args": "",
     "summary": "Record a plan task (T-NN)."},
    {"name": "task start", "group": "workflow", "args": "<T-NN>",
     "summary": "Mark a task in_progress."},
    {"name": "task done", "group": "workflow", "args": "<T-NN>",
     "summary": "Mark a task done."},
    {"name": "task block", "group": "workflow", "args": "<T-NN>",
     "summary": "Mark a task blocked."},
    {"name": "task reopen", "group": "workflow", "args": "<T-NN>",
     "summary": "Return a task to pending."},
    {"name": "task list", "group": "workflow", "args": "",
     "summary": "List tasks, progress, and the next actionable."},
    {"name": "task show", "group": "workflow", "args": "[<T-NN>]",
     "summary": "Show a task's brief (acceptance + cited REQ-NN + constraints)."},
]


def build_guide(root: Path | None, cfg: SpecfloConfig | None) -> dict:
    """Assemble the guide payload for the current repo state.

    ``root``/``cfg`` are the already-resolved repo root and config, or ``None``
    when the repo is not a specflo project. Read-only; never mutates.
    """
    payload: dict = {
        "pipeline": list(workflow.PHASES),
        "commands": [dict(entry) for entry in COMMANDS],
    }

    if root is None or cfg is None:
        payload["initialized"] = False
        payload["next_action"] = "init"
        return payload

    payload["initialized"] = True

    if cfg.active_project is None:
        payload["active_project"] = None
        payload["next_action"] = "new"
        return payload

    try:
        project = projects.load_project(root, cfg, cfg.active_project)
    except SpecfloError:
        # Config names an active project that won't load — stay useful by
        # pointing at `new`/`switch` rather than failing.
        payload["active_project"] = None
        payload["next_action"] = "new"
        return payload

    payload["active_project"] = project.slug
    payload["phase"] = project.phase
    payload["next_step"] = workflow.next_step(project.phase)
    payload["next_action"] = project.phase
    return payload
