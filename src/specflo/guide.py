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

from . import index, projects, workflow
from .config import SpecfloConfig
from .errors import SpecfloError

# A thin, version-less section users paste once near the top of their agent
# memory file (CLAUDE.md / AGENTS.md / GEMINI.md / ...). Deliberately static: no
# version, so it never goes stale and never needs re-committing when specflo is
# upgraded. Its only job is to make specflo *discoverable* to a cold agent — the
# live detail (command surface, next action) lives behind `specflo guide`, which
# the snippet points at, so it never has to be duplicated here.
#
# README.md is the authority for this text: it is the copy users actually read
# while onboarding, and this constant mirrors it. `tests/test_guide.py` extracts
# the README block and asserts the two are byte-identical, so editing README
# alone fails the suite instead of silently drifting.
MEMORY_SNIPPET = (
    "## Development workflow\n"
    "\n"
    "This repo uses specflo for feature development. Run `specflo guide` at the\n"
    "start of a session to orient yourself; `specflo status` shows the active\n"
    "project and phase. Features move through brainstorm -> spec -> plan -> execute\n"
    "using the specflo skills, recording decisions, requirements, and tasks through\n"
    "the specflo CLI rather than editing its artifacts by hand."
)

# Curated command table. ``name`` is the canonical command path (matched against
# the live CLI by the coverage guard); ``args`` is the metavar shown to humans.
COMMANDS: list[dict[str, str]] = [
    {"name": "init", "group": "setup", "args": "",
     "summary": "Scaffold .specflo/ and the projects dir."},
    {"name": "new", "group": "setup", "args": "<name>",
     "summary": "Create a project and make it active."},
    {"name": "list", "group": "setup", "args": "",
     "summary": "List all projects, marking the active one."},
    {"name": "index", "group": "setup", "args": "",
     "summary": "(Re)generate specflo-index.md, the ledger of every project."},
    {"name": "summary", "group": "setup", "args": "[<name>] <text>",
     "summary": "Set a project's one-line summary and refresh the index."},
    {"name": "execution", "group": "setup", "args": "linear|fan-out",
     "summary": "Switch the active project's execution mode (any phase)."},
    {"name": "switch", "group": "setup", "args": "<name>",
     "summary": "Make another project active."},
    {"name": "leave", "group": "setup", "args": "",
     "summary": "Leave the active project: clear the pointer, change nothing else."},
    {"name": "shelve", "group": "setup", "args": "[<name>]",
     "summary": "Shelve a project (pause it); status -> shelved, phase kept."},
    {"name": "resume", "group": "setup", "args": "[<name>]",
     "summary": "Resume a shelved project; status -> active, make it active."},
    {"name": "status", "group": "setup", "args": "",
     "summary": "Show the active project, its phase, and what's next."},
    {"name": "guide", "group": "setup", "args": "",
     "summary": "Show this overview of specflo and what to do next."},
    {"name": "checkpoint", "group": "setup", "args": "",
     "summary": "Print the resume prompt; refresh checkpoint.md."},
    {"name": "config get", "group": "setup", "args": "<key>",
     "summary": "Print one setting's resolved value."},
    {"name": "config list", "group": "setup", "args": "",
     "summary": "Show every setting, its value, and where that value came from."},
    {"name": "config set", "group": "setup", "args": "<key> <value>",
     "summary": "Set one setting, validated before it is written."},
    {"name": "config unset", "group": "setup", "args": "<key>",
     "summary": "Drop one setting, returning it to its shipped default."},
    {"name": "hook reseed", "group": "setup", "args": "",
     "summary": "Emit the session-start reseed payload (used by the SessionStart hook)."},
    {"name": "hook install", "group": "setup", "args": "",
     "summary": "Wire the SessionStart hook into Claude Code's .claude/settings.json (idempotent merge)."},
    {"name": "hook print", "group": "setup", "args": "",
     "summary": "Print the Claude Code SessionStart wiring fragment (see: hook install)."},
    {"name": "auto", "group": "setup", "args": "",
     "summary": "Emit the auto-mode handoff payload (opt-in unattended run)."},
    {"name": "extension install", "group": "setup", "args": "[--scope user|project]",
     "summary": "Install the bundled pi extension into pi's extension directory."},
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
    {"name": "reopen", "group": "workflow", "args": "[<phase>]",
     "summary": "Move the phase pointer back to an earlier phase (undo an advance)."},
    {"name": "plan start", "group": "workflow", "args": "",
     "summary": "Create the plan.md artifact."},
    {"name": "plan graph", "group": "workflow", "args": "",
     "summary": "Render the execution graph: waves, tasks, and a mermaid block."},
    {"name": "task add", "group": "workflow", "args": "",
     "summary": "Record a plan task (T-NN)."},
    {"name": "task rewire", "group": "workflow", "args": "",
     "summary": "Repoint dependents of one task onto another (--from/--to)."},
    {"name": "task start", "group": "workflow", "args": "<T-NN>",
     "summary": "Mark a task in_progress."},
    {"name": "task done", "group": "workflow", "args": "<T-NN>",
     "summary": "Mark a task done."},
    {"name": "task block", "group": "workflow", "args": "<T-NN>",
     "summary": "Mark a task blocked."},
    {"name": "task reopen", "group": "workflow", "args": "<T-NN>",
     "summary": "Return a task to pending."},
    {"name": "task edit", "group": "workflow", "args": "<T-NN>",
     "summary": "Edit an active task's fields and dependencies in place."},
    {"name": "task note", "group": "workflow", "args": "<T-NN>",
     "summary": "Append a dated note to a task (--text, --label)."},
    {"name": "task list", "group": "workflow", "args": "",
     "summary": "List tasks, progress, and the next actionable."},
    {"name": "task show", "group": "workflow", "args": "[<T-NN>]",
     "summary": "Show a task's brief (acceptance + cited REQ-NN + constraints)."},
    {"name": "task set-milestone", "group": "workflow", "args": "<T-NN> <M-NN>",
     "summary": "Assign or reassign a task's milestone."},
    {"name": "milestone add", "group": "workflow", "args": "",
     "summary": "Group tasks into a milestone (M-NN) with an Exit checklist."},
    {"name": "milestone list", "group": "workflow", "args": "",
     "summary": "List milestones with done/total rollup and the current one."},
    {"name": "milestone show", "group": "workflow", "args": "<M-NN>",
     "summary": "Show a milestone's Exit checklist, member tasks, and REQ set."},
    {"name": "pool add", "group": "workflow", "args": "<name>",
     "summary": "Declare or resize a resource pool (--size N) in plan.md."},
    {"name": "pool list", "group": "workflow", "args": "",
     "summary": "List declared and needed pools with their slot sizes."},
    {"name": "review start", "group": "workflow", "args": "",
     "summary": "Mint the next review round (review-N.md) and print its path."},
    {"name": "review done", "group": "workflow", "args": "",
     "summary": "Close the open review round with a verdict (--verdict)."},
    {"name": "agent start", "group": "agents", "args": "<name>",
     "summary": "Start a detached pi agent host in a herdr tab when available "
                "(--cwd, --pi-cmd, --workspace, --no-herdr)."},
    {"name": "agent status", "group": "agents", "args": "<name>",
     "summary": "Live-checked agent status; --json adds the state-dir paths."},
    {"name": "agent list", "group": "agents", "args": "",
     "summary": "List known agents with their live-checked states."},
    {"name": "agent prompt", "group": "agents", "args": "<name> <text>",
     "summary": "Send a prompt; block until settle and print the reply "
                "(--timeout, --no-wait, --steer, --follow-up)."},
    {"name": "agent wait", "group": "agents", "args": "<name>",
     "summary": "Block until the agent's current run settles."},
    {"name": "agent last", "group": "agents", "args": "<name>",
     "summary": "Print the most recent final assistant text."},
    {"name": "agent log", "group": "agents", "args": "<name>",
     "summary": "Print the agent's event log; --follow streams new events."},
    {"name": "agent stop", "group": "agents", "args": "<name>",
     "summary": "Gracefully stop an agent: abort its run, terminate pi, "
                "then the host."},
]


def build_guide(root: Path | None, cfg: SpecfloConfig | None) -> dict:
    """Assemble the guide payload for the current repo state.

    ``root``/``cfg`` are the already-resolved repo root and config, or ``None``
    when the repo is not a specflo project. Read-only; never mutates.
    """
    payload: dict = {
        "pipeline": list(workflow.PHASES),
        "commands": [dict(entry) for entry in COMMANDS],
        "memory_snippet": MEMORY_SNIPPET,
    }

    if root is None or cfg is None:
        payload["initialized"] = False
        payload["next_action"] = "init"
        return payload

    payload["initialized"] = True
    # The prior-projects rule (project-index REQ-06): present only while a
    # completed project exists, worded by the configured mode.
    payload["rule"] = index.rule_line(root, cfg)

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
