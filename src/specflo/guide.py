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
    "The user develops features with specflo. In a repo that has a `.specflo/`\n"
    "directory, run `specflo guide` at the start of a session to orient yourself;\n"
    "`specflo status` shows the active project and phase. Features move through\n"
    "brainstorm -> spec -> plan -> execute using the specflo skills, recording\n"
    "decisions, requirements, and tasks through the specflo CLI rather than editing\n"
    "its artifacts by hand."
)

# Where the snippet goes is the user's call: the guide tells the agent to ask.
# The README section lists the same three places.
MEMORY_PLACEMENT = (
    "Add this section once near the top of an agent memory file, so a fresh agent\n"
    "knows specflo is here. Ask the user which file:\n"
    "  global  ~/.claude/CLAUDE.md or the harness's user file: every repo\n"
    "  local   CLAUDE.local.md in the repo root, gitignored: this repo, only you\n"
    "  team    the repo's CLAUDE.md or AGENTS.md, committed: everyone in the repo"
)

# No active project is a state, not a prompt: the same two lines serve `guide`,
# `status`, and the error from any command that needs an active project. They
# name both ways in - a new project or an existing one - and nudge toward
# neither. Add nothing here that reads as "create one".
NO_ACTIVE_PROJECT_LINES = (
    "No active project.",
    "Enter one with `specflo new <name>` or `specflo switch <name>`.",
)
NO_ACTIVE_PROJECT_MESSAGE = " ".join(NO_ACTIVE_PROJECT_LINES)

# Curated command table. ``name`` is the canonical command path (matched against
# the live CLI by the coverage guard); ``args`` is the metavar shown to humans.

COMMANDS: list[dict[str, str]] = [
    {"name": "init", "group": "setup", "args": "",
     "summary": "Scaffold .specflo/ and the projects dir."},
    {"name": "doctor", "group": "setup", "args": "",
     "summary": "Check specflo is on PATH and each agent harness has the skills."},
    {"name": "skills install", "group": "setup", "args": "[--scope user|project] [--harness NAME[:SCOPE]]",
     "summary": "Install the bundled workflow skills into the detected agent harnesses."},
    {"name": "skills status", "group": "setup", "args": "",
     "summary": "Show each bundled skill's state in each harness."},
    {"name": "skills update", "group": "setup", "args": "",
     "summary": "Update installed skills that are older than the bundled ones."},
    {"name": "skills uninstall", "group": "setup", "args": "",
     "summary": "Remove the skills that specflo installed."},
    {"name": "hook install", "group": "setup", "args": "",
     "summary": "Wire the SessionStart hook into Claude Code's .claude/settings.json (idempotent merge)."},
    {"name": "hook print", "group": "setup", "args": "",
     "summary": "Print the Claude Code SessionStart wiring fragment (see: hook install)."},
    {"name": "hook reseed", "group": "setup", "args": "",
     "summary": "Emit the session-start reseed payload (used by the SessionStart hook)."},
    {"name": "extension install", "group": "setup", "args": "[--scope user|project]",
     "summary": "Install the bundled pi extension into pi's extension directory."},
    {"name": "config get", "group": "setup", "args": "<key>",
     "summary": "Print one setting's resolved value."},
    {"name": "config list", "group": "setup", "args": "",
     "summary": "Show every setting, its value, and where that value came from."},
    {"name": "config set", "group": "setup", "args": "<key> <value>",
     "summary": "Set one setting, validated before it is written."},
    {"name": "config unset", "group": "setup", "args": "<key>",
     "summary": "Drop one setting, returning it to its shipped default."},
    {"name": "new", "group": "projects", "args": "<name> [--level quick|fast|full|harden]",
     "summary": "Create a project and make it active."},
    {"name": "list", "group": "projects", "args": "",
     "summary": "List all projects, marking the active one."},
    {"name": "status", "group": "projects", "args": "",
     "summary": "Show the active project, its phase, and what's next."},
    {"name": "guide", "group": "projects", "args": "[daemon]",
     "summary": "Show this overview of specflo and what to do next; `daemon` lists "
                "the daemon and team commands."},
    {"name": "switch", "group": "projects", "args": "<name>",
     "summary": "Make another project active."},
    {"name": "leave", "group": "projects", "args": "",
     "summary": "Leave the active project: clear the pointer, change nothing else."},
    {"name": "shelve", "group": "projects", "args": "[<name>]",
     "summary": "Shelve a project (pause it); status -> shelved, phase kept."},
    {"name": "resume", "group": "projects", "args": "[<name>]",
     "summary": "Resume a shelved project; status -> active, make it active."},
    {"name": "level", "group": "projects", "args": "fast|full",
     "summary": "Move the active project up a level; it goes back to brainstorm. "
                "A harden project never moves."},
    {"name": "execution", "group": "projects", "args": "linear|fan-out",
     "summary": "Switch the active project's execution mode (any phase)."},
    {"name": "egress", "group": "projects", "args": "local|no-train|open",
     "summary": "Pin the active project's egress class (any phase)."},
    {"name": "summary", "group": "projects", "args": "[<name>] <text>",
     "summary": "Set a project's one-line summary and refresh the index."},
    {"name": "index", "group": "projects", "args": "",
     "summary": "(Re)generate specflo-index.md, the ledger of every project."},
    {"name": "checkpoint", "group": "projects", "args": "",
     "summary": "Print the resume prompt; refresh checkpoint.md."},
    {"name": "auto", "group": "projects", "args": "",
     "summary": "Emit the auto-mode handoff payload (opt-in unattended run)."},
    {"name": "brainstorm start", "group": "brainstorm", "args": "",
     "summary": "Create the brainstorm.md artifact."},
    {"name": "decision add", "group": "brainstorm", "args": "[--brief <B-NN>] [--diverges]",
     "summary": "Record a decision (D-NN) in the brainstorm, or in a brief; --diverges "
                "marks an approved divergence from the reference design."},
    {"name": "decision list", "group": "brainstorm", "args": "[--diverges] [--all]",
     "summary": "List the decisions across the brainstorm and every brief."},
    {"name": "brief add", "group": "execute", "args": "<title>",
     "summary": "Start a brief (B-NN) inside a running project for one feature's "
                "ask, facts, decisions and contract."},
    {"name": "brief set", "group": "execute", "args": "<B-NN> <section>",
     "summary": "Replace one prose section of a brief (--file or --stdin)."},
    {"name": "spec start", "group": "spec", "args": "",
     "summary": "Create the spec.md artifact."},
    {"name": "requirement add", "group": "spec", "args": "",
     "summary": "Record a spec requirement (REQ-NN)."},
    {"name": "plan start", "group": "plan", "args": "",
     "summary": "Create the plan.md artifact."},
    {"name": "task add", "group": "plan", "args": "[--fixes <F-NN>]",
     "summary": "Record a plan task (T-NN); --fixes names a review finding it fixes "
                "(a harden project's only kind of task)."},
    {"name": "task edit", "group": "plan", "args": "<T-NN>",
     "summary": "Edit an active task's fields and dependencies in place."},
    {"name": "task note", "group": "plan", "args": "<T-NN>",
     "summary": "Append a dated note to a task (--text, --label)."},
    {"name": "task rewire", "group": "plan", "args": "",
     "summary": "Repoint dependents of one task onto another (--from/--to)."},
    {"name": "milestone add", "group": "plan", "args": "",
     "summary": "Group tasks into a milestone (M-NN) with an Exit checklist."},
    {"name": "milestone list", "group": "plan", "args": "",
     "summary": "List milestones with done/total rollup and the current one."},
    {"name": "milestone show", "group": "plan", "args": "<M-NN>",
     "summary": "Show a milestone's Exit checklist, member tasks, and REQ set."},
    {"name": "task set-milestone", "group": "plan", "args": "<T-NN> <M-NN>",
     "summary": "Assign or reassign a task's milestone."},
    {"name": "pool add", "group": "plan", "args": "<name>",
     "summary": "Declare or resize a resource pool (--size N) in plan.md."},
    {"name": "pool list", "group": "plan", "args": "",
     "summary": "List declared and needed pools with their slot sizes."},
    {"name": "plan graph", "group": "plan", "args": "",
     "summary": "Render the execution graph: waves, tasks, and a mermaid block."},
    {"name": "task list", "group": "execute", "args": "",
     "summary": "List tasks, progress, and the next actionable."},
    {"name": "task show", "group": "execute", "args": "[<T-NN>]",
     "summary": "Show a task's brief (acceptance + cited REQ-NN + constraints)."},
    {"name": "task start", "group": "execute", "args": "<T-NN>",
     "summary": "Mark a task in_progress."},
    {"name": "task done", "group": "execute", "args": "<T-NN> [--closes <FU-NN>]",
     "summary": "Mark a task done; --closes closes a follow-up it settles."},
    {"name": "task block", "group": "execute", "args": "<T-NN>",
     "summary": "Mark a task blocked."},
    {"name": "task reopen", "group": "execute", "args": "<T-NN>",
     "summary": "Return a task to pending."},
    {"name": "review start", "group": "review", "args": "[--full] [--over-budget] [--harden] [--brief <B-NN>]",
     "summary": "Mint the next review round (review-N.md) and print its scope; --harden "
                "opens a harden round: a fresh review of the whole scope, outside the "
                "round budget."},
    {"name": "review prompt", "group": "review", "args": "",
     "summary": "Print the reviewer brief for the open round."},
    {"name": "review finding add", "group": "review",
     "args": "--severity <s> --text <t> [--at <file>:<line>[-<line>]]",
     "summary": "Record a finding (F-NN), severity blocker|should-fix|nit; --at names where "
                "the defect is, needed for blocker and should-fix."},
    {"name": "review finding check", "group": "review", "args": "<F-NN> <closed|open>",
     "summary": "Record whether an earlier blocker or should-fix item is fixed."},
    {"name": "review finding reject", "group": "review", "args": "<F-NN> --reason <why>",
     "summary": "Reject an open blocker or should-fix item: no round checks it, no task fixes it."},
    {"name": "review finding defer", "group": "review", "args": "<F-NN> --do <what>",
     "summary": "Defer an open blocker or should-fix item to a follow-up (FU-NN); not for a "
                "hosted project."},
    {"name": "review done", "group": "review", "args": "",
     "summary": "Close the open review round; the verdict comes from its findings."},
    {"name": "review waive", "group": "review", "args": "",
     "summary": "Close the open round waived, or record a waived round (--reason)."},
    {"name": "validate", "group": "any", "args": "<artifact>",
     "summary": "Lint an artifact/phase (brainstorm|spec|plan|execute)."},
    {"name": "advance", "group": "any", "args": "",
     "summary": "Gate the current artifact, then move to the next phase."},
    {"name": "reopen", "group": "any", "args": "[<phase>]",
     "summary": "Move the phase pointer back to an earlier phase (undo an advance)."},
    {"name": "doc show", "group": "any", "args": "<artifact>",
     "summary": "Print an artifact of the active project verbatim "
                "(brainstorm|spec|plan|brief|checkpoint|project|followup)."},
    {"name": "section set", "group": "any", "args": "<artifact> <section>",
     "summary": "Replace one prose section's body from --file or --stdin "
                "(managed sections refused)."},
    {"name": "followup add", "group": "any", "args": "<title> --do <text> [--from <text>]",
     "summary": "Record work the project leaves for a later one (FU-NN)."},
    {"name": "followup list", "group": "any", "args": "[--all] [--json]",
     "summary": "List the open follow-ups of every project."},
    {"name": "followup show", "group": "any", "args": "<FU-NN> [--json]",
     "summary": "Show one follow-up from any project: Do, From, Status, Closed and Closed by."},
    {"name": "followup close", "group": "any", "args": "<FU-NN> --note <text> [--by <ref>]",
     "summary": "Close an open follow-up in any project, with a note and what did the work."},
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
    {"name": "agent reset", "group": "agents", "args": "<name>",
     "summary": "Clear the agent's conversation context in place (pi "
                "new_session); under a lease only the holder may."},
    {"name": "agent stop", "group": "agents", "args": "<name>",
     "summary": "Gracefully stop an agent: abort its run, terminate pi, "
                "then the host."},
    {"name": "serve", "group": "daemon", "args": "--root <dir>",
     "summary": "Run the daemon that hosts projects for CLI clients (serve extra)."},
    {"name": "serve token add", "group": "daemon", "args": "--root <dir> ... <requester|developer|agent>",
     "summary": "Mint a daemon bearer token for one identity; the secret prints once."},
    {"name": "serve pool validate", "group": "daemon", "args": "--root <dir> ...",
     "summary": "Check the pool configuration under the daemon root; prints every fault in one run."},
    {"name": "serve pool init", "group": "daemon", "args": "--root <dir> ...",
     "summary": "Write a starting pool directory: a commented pool.yaml and the shipped agent definitions; keeps existing files."},
    {"name": "serve pool reload", "group": "daemon", "args": "--root <dir> ... [--remote <name>]",
     "summary": "Ask the running daemon, as the developer, to read its pool directory again; a directory with faults changes nothing."},
    {"name": "remote add", "group": "daemon", "args": "<name> <url> --token <secret>",
     "summary": "Register a daemon by name; changes nothing about local projects."},
    {"name": "remote list", "group": "daemon", "args": "",
     "summary": "List the registered daemons: names and URLs, never tokens."},
    {"name": "remote remove", "group": "daemon", "args": "<name>",
     "summary": "Forget a registered daemon."},
    {"name": "promote", "group": "daemon", "args": "<project> --remote <name>",
     "summary": "Move a local project into a daemon: upload, verify, remove the local copy."},
    {"name": "product add", "group": "daemon", "args": "<name> [--slug <slug>] [--repo <location>] [--remote <name>]",
     "summary": "Add a product to a daemon: name, slug, and repo location; a taken slug is refused."},
    {"name": "product list", "group": "daemon", "args": "[--remote <name>] [--json]",
     "summary": "List a daemon's products: slug, name, and repo location."},
    {"name": "product show", "group": "daemon", "args": "<slug> [--remote <name>] [--json]",
     "summary": "Show one product: name, slug, repo location, created date, and vision."},
    {"name": "product set-vision", "group": "daemon", "args": "<slug> [<text>] [--stdin] [--remote <name>]",
     "summary": "Replace a product's vision text, given inline or on stdin."},
    {"name": "product roadmap", "group": "daemon", "args": "<slug> [--remote <name>] [--json]",
     "summary": "Print a product's roadmap: the vision, then its backlog in order."},
    {"name": "product piece add", "group": "daemon", "args": "<product> <piece> [--remote <name>]",
     "summary": "Declare a piece (web, admin, ...) on a product; its work items may target it."},
    {"name": "product piece list", "group": "daemon", "args": "<product> [--remote <name>] [--json]",
     "summary": "List a product's declared pieces."},
    {"name": "product piece remove", "group": "daemon", "args": "<product> <piece> [--remote <name>]",
     "summary": "Drop a declared piece; one a work item targets stays."},
    {"name": "workitem add", "group": "daemon", "args": "<product> <title> [--kind <kind>] [--issue <link>] [--dev-path full|one-prompt|cyclical] [--piece <piece>] [--remote <name>]",
     "summary": "Add a work item to a product's backlog; a dev path outside the three is refused."},
    {"name": "workitem list", "group": "daemon", "args": "[--product <slug>] [--status <status>] [--kind <kind>] [--remote <name>] [--json]",
     "summary": "List work items in backlog order, filtered by product, status, or kind."},
    {"name": "workitem show", "group": "daemon", "args": "<id> [--remote <name>] [--json]",
     "summary": "Show one work item: product, kind, dev path, status, issue link, created date."},
    {"name": "workitem set-status", "group": "daemon", "args": "<id> open|in-progress|done|dropped [--remote <name>]",
     "summary": "Move a work item to another status."},
    {"name": "workitem spawn", "group": "daemon", "args": "<id> [--name <name>] [--remote <name>]",
     "summary": "Spawn the one project a full-path work item gets, hosted beside it; it becomes active."},
    {"name": "gate open", "group": "daemon", "args": "<requester|developer> [--note <text>]",
     "summary": "Hand the active project to a role: it waits until someone takes the gate."},
    {"name": "gate take", "group": "daemon", "args": "[--by <requester|developer>]",
     "summary": "Take the active project's open gate; --by only when the agent relays a human."},
    {"name": "lease request", "group": "daemon", "args": "<pool>",
     "summary": "Lease a member of a daemon's agent pool; prints the lease id and the "
                "agent to drive with `agent` (--cwd, --idle-limit, --remote). Waits up to "
                "600 s for a full pool and says so at once on stderr; interrupt it to "
                "cancel, or pass --wait 0 to be refused at once (--wait <seconds>)."},
    {"name": "lease release", "group": "daemon", "args": "<lease>",
     "summary": "Give a lease back to its pool; only its holder may, and a lease "
                "that has ended is reported as it ended."},
    {"name": "lease list", "group": "daemon", "args": "",
     "summary": "List the leases this checkout holds tokens for; no other "
                "orchestrator's lease is shown (--remote, --json)."},
    {"name": "console attach", "group": "daemon", "args": "<slot> <agent>",
     "summary": "Attach your own running rpc agent on the daemon's host to a console "
                "slot the pool declares; the developer identity only (--remote, --json)."},
    {"name": "console detach", "group": "daemon", "args": "<slot>",
     "summary": "Detach a console slot: it takes no new lease, and a lease that is "
                "out on it stands (--remote, --json)."},
]


# How much ceremony to pick, before `specflo new`: the agent proposes a level
# from the request and the user confirms it.
LEVELS_TEXT = (
    "Levels:   propose one from the request, and have the user confirm it before"
    " `specflo new`:\n"
    "  quick   one goal and one check (`new <name> --level quick`): one brief, proof, no review\n"
    "  fast    3 to 7 tasks (`--level fast`): short brainstorm, spec and plan, one approval\n"
    "  full    more than that (the default): every phase, an approval at each\n"
    "  harden  make code that already exists sound (`--level harden`): harden rounds,"
    " fix tasks only, the user stops it\n"
    "  A project moves up with `specflo level fast|full`, never down; a harden project"
    " never moves.\n"
    "  When the user names a follow-up (FU-NN) to do, read it with"
    " `specflo followup show FU-NN` before you propose a level."
)


def build_guide(root: Path | None, cfg: SpecfloConfig | None) -> dict:
    """Assemble the guide payload for the current repo state.

    ``root``/``cfg`` are the already-resolved repo root and config, or ``None``
    when the repo is not a specflo project. Read-only; never mutates.
    """
    payload: dict = {
        "pipeline": list(workflow.PHASES),
        "commands": [dict(entry) for entry in COMMANDS],
        "memory_snippet": MEMORY_SNIPPET,
        "memory_placement": MEMORY_PLACEMENT,
        "levels": LEVELS_TEXT,
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
        payload["next_action"] = "none"
        return payload

    try:
        project = projects.load_project(root, cfg, cfg.active_project)
    except SpecfloError:
        # Config names an active project that won't load — stay useful by
        # pointing at `new`/`switch` rather than failing.
        payload["active_project"] = None
        payload["next_action"] = "none"
        return payload

    payload["active_project"] = project.slug
    payload["phase"] = project.phase
    # auto imports much of the package, so it is read here, at call time.
    from . import auto, plan, review, validators

    complete = project.status == projects.COMPLETE_STATUS
    # The same inputs status reads, so fast level's one approval reads alike.
    validates = False
    if project.phase in ("brainstorm", "spec", "plan") and not complete:
        validator = validators.VALIDATORS.get(project.phase)
        if validator is not None:
            validates = not validator(root, cfg, project.slug)
    payload["next_step"] = (
        auto.ladder_step(root, cfg, project)
        or auto.ladder_full_step(root, cfg, project)
        or workflow.next_step(
            project.phase, level=project.level, complete=complete, validates=validates,
            unattended=auto.run_under_way(root, cfg, project),
            # The inputs status reads at execute, so the review-aware hint
            # reads alike on both surfaces.
            progress=(
                plan.plan_progress(root, cfg, project.slug)
                if project.phase == "execute" else None
            ),
            review=review.review_state(root, cfg, project.slug),
            test_command=cfg.test_command,
        )
    )
    payload["next_action"] = project.phase
    return payload
