# specflo

[![PyPI](https://img.shields.io/pypi/v/specflo)](https://pypi.org/project/specflo/)
[![Python versions](https://img.shields.io/pypi/pyversions/specflo)](https://pypi.org/project/specflo/)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](https://github.com/TacoTakumi/specflo/blob/main/LICENSE)

**Spec-driven software engineering for coding agents.** specflo is a Python CLI
plus a set of skills and hooks that give an AI agent a disciplined
brainstorm -> spec -> plan -> execute loop, with every artifact (specs, plans,
decisions, tasks) written to plain markdown on disk instead of living in the
model's context window.

- **State lives in files, not in the model's head.** Phase state, validation
  gates, and one-task-at-a-time focus live in the CLI and on disk. That makes
  agentic development more reliable, and it makes real development practical
  with capable but smaller *local* models such as Qwen3.6 27B: the agent only
  has to reason about the next small, well-scoped, validated step, not hold an
  entire project in context.
- **Multi-project by design.** specflo tracks many projects in one repo with a
  single active project and switch-anytime, so a monorepo can carry several
  concurrent efforts without them stepping on each other. Each project keeps
  its own phase, artifacts, and resume checkpoint.
- **Pluggable into any harness that can run a CLI and read markdown.** Claude
  Code and [pi] are integrated out of the box (skills, session-start hook, a
  bundled pi extension); opencode and Hermes get the skills; anything else can
  drive the plain CLI.

specflo is built with itself: every feature since v0.1 has gone through its own
brainstorm -> spec -> plan -> execute pipeline. The [CHANGELOG](https://github.com/TacoTakumi/specflo/blob/main/CHANGELOG.md)
is the development history. Pre-1.0, interfaces may still move; breaking
changes are called out explicitly in the changelog.

**Three surfaces are a heavy work in progress and change constantly: pi
subagents (`specflo agent`), daemon hosting (`specflo serve`) and the agent
pool (`specflo lease`, `specflo console`).** They are written up below because
they work and are being used, not because they are settled. Expect their
commands, flags, on-disk formats and behaviour to move between releases, with
no migration path.

## Quick start

Requires Python 3.12+. Install from PyPI:

```bash
uv tool install specflo    # or: pipx install specflo / pip install specflo
```

Then set up the repo you want to work in:

```bash
specflo init                # scaffold .specflo/ config + the projects dir
specflo skills install      # install the workflow skills into your agent harness(es)
specflo hook install        # Claude Code: session-resume wiring (recommended)
specflo extension install   # pi: same session-resume wiring, as a pi extension
```

The last two lines are per-harness alternatives - run the one that matches your
agent, or neither if it is some other harness.

That is the whole setup. Start your coding agent and say:

> We use specflo here. Start a new specflo project for <the thing to build>.

The specflo-brainstorm skill takes over - one question at a time, decisions recorded on
disk - and hands off through spec and plan to task-by-task execution. Every
step also works without an agent; see the
[command reference](#command-reference).

## Using specflo with your agent

The installed skills make your agent *able* to drive specflo; a note in the
project memory file (`CLAUDE.md`, `AGENTS.md`, ...) makes it *routine*. Paste
this near the top of that file:

```markdown
## Development workflow

This repo uses specflo for feature development. Run `specflo guide` at the
start of a session to orient yourself; `specflo status` shows the active
project and phase. Features move through brainstorm -> spec -> plan -> execute
using the specflo skills, recording decisions, requirements, and tasks through
the specflo CLI rather than editing its artifacts by hand.
```

Prompts that map onto the workflow:

- "Start a new specflo project for X." - creates the project, opens the brainstorm.
- "continue" - after a fresh start or a context clear, resume from the checkpoint.
- "Where are we?" - the active project's phase and next step (`specflo status`).
- "Park this for now." / "Pick that back up." - shelve and resume a project.
- "Run it in auto mode." - explicit opt-in to an unattended run (see `specflo auto`).

`specflo guide` runs cold - before `init`, in any repo - and orients a fresh
agent in one shot: what specflo is, the pipeline, the full command surface, and
what to do next. When in doubt, tell your agent to run it.

## The pipeline

Four phases, each gated by a validated artifact:

1. **brainstorm** - capture the idea; resolve open questions into recorded decisions (`D-NN`).
2. **spec** - synthesize testable requirements (`REQ-NN`), each traced to the decisions behind it.
3. **plan** - decompose into dependency-ordered tasks (`T-NN`), each with an acceptance criterion, a verify step, and the requirements it implements; optionally grouped into milestones (`M-NN`).
4. **execute** - work tasks one at a time; a completion gate confirms every task is done *and* that the final whole-branch review was recorded with a passing verdict before the project completes.

`specflo advance` validates the current phase's artifact before moving on, so a
hole in the spec stops the line early instead of surfacing mid-execution.

The end-of-execute review is an artifact too. `specflo review start` mints a
numbered `review-N.md`; `specflo review done --verdict ...` closes it with one of
`ready-to-merge`, `changes-requested` or `waived`. Completing the project
requires the latest round to have closed with a passing verdict, so a review that
happened in some cleared context is no longer something you have to remember.

Artifacts are plain markdown under `docs/projects/<slug>/` (configurable):

```text
$ specflo status
Project: Payment retries (payment-retries)
Dir:     docs/projects/payment-retries
Phase:   brainstorm
Execution: linear
Next:    Brainstorm and research; capture decisions, then write the spec.
Resume:  specflo checkpoint
```

Clearing context is free at any point: `checkpoint.md` is rewritten after every
state change, and `specflo checkpoint` prints the resume prompt that puts a
fresh session back to work.

## Levels

Not every change needs the whole pipeline. A project has one of three levels,
chosen with `specflo new <name> --level quick|fast|full`:

- **quick** - one goal and one check. The project has one phase, `execute`, and
  one document, `brief.md`, with the sections Goal, Done when, Proof and
  Deferred. `specflo validate brief` asks for a goal, exactly one check and
  proof, and `specflo advance` completes the project once it passes. There is no
  review round: the check and its proof are the gate.
- **fast** - 3 to 7 tasks. The usual brainstorm, spec and plan, written short by
  the agent: it makes at most 3 decisions itself, keeps going from brainstorm
  to plan without stopping, and stops once, when the plan validates, for the
  user's approval before execute. More than 3 active decisions or 7 active
  tasks only warns: the agent adds no more work and defers new work, and the
  project stays at fast. Execute finishes as at full level, after a passing review round.
- **full** - the default: every phase, with an approval at each.

`specflo doc show brief` prints one page at either light level: the brief file
at quick, and at fast the goal, decisions, checks and tasks drawn from the three
documents. Nothing is stored for the fast view.

A project moves up with `specflo level fast|full`, never down, and goes back to
the brainstorm with every document and every done task kept. From quick, the
brief seeds the three documents: the goal and the deferred items go into the
brainstorm, the check becomes `REQ-01`, and `T-01` implements it (done when the
brief has proof). From fast, the command lists the decisions to review with the
user, since the agent made them without an interview.

A cap never stops a `specflo auto` run and never moves the project up. A quick
brief with two checks is told to keep one and defer the rest. A fast project
past its caps only warns, since no command removes a decision or task. Moving
up is your choice, with `specflo level`.

### The ladder run

`specflo auto --ladder` on a quick project runs all three levels in one
unattended run, each on its own local branch:

1. The quick level runs on `specflo/<slug>/quick`, cut from where you started.
2. When it completes, the next pass cuts `specflo/<slug>/fast` from there and
   moves the project up; the fast level builds on the quick result.
3. Then `specflo/<slug>/full` the same way. With no user to interview, the
   agent reviews each fast decision itself, takes up the deferred list, and
   must close a review round of its own, which the climb opens.
4. After full completes, one more `specflo auto` pass writes the last row and
   stops, naming the three branches.

At every level `specflo advance` ends only that level; the texts it and
`status` print say to run `specflo auto` again, and `status --json` keeps
reporting the run as under way until the closing pass.

Inside a ladder, caps work as in any auto run, and the next level picks up the
deferred work. A fast branch can be larger than fast allows. While a
ladder runs, `specflo level` is refused: the ladder moves the level up itself.
A level's row counts commits up to its branch tip. `ladder.md` in the project
directory records the base branch and commit and one row per level: commits, files and
lines changed against the level's start, tasks, test result, review verdict,
deferred items and time. The test result comes from the optional `test_command`
config key (`specflo config set test_command "uv run pytest -q"`), run on each
level's branch; unset, the row says `not run`. Review the three branches and
merge the one you like best. The ladder needs a clean tree (specflo's own
documents excepted), never pushes, and never deletes, renames or resets a
branch.

## Command reference

### Global option: run in another directory

- `specflo -C DIR <command>` / `specflo --directory DIR <command>` - run the command as if specflo had been started in `DIR`. Every command honors it, including `init`, `hook reseed`, `auto`, and `extension install --scope project`; root discovery is the usual walk up from `DIR` to the nearest `.specflo/config.yaml`. `DIR` must exist and be a directory, or the command exits 2 before anything runs. The option goes before the subcommand.
- `SPECFLO_DIRECTORY=DIR` - the environment variable has the same effect. Precedence is the flag, then the env var, then the current directory; a set-but-empty variable counts as unset.
- Relative path arguments given to the subcommand (`review done --file report.md`, `init --projects-dir custom`) resolve against `DIR`, not the caller's cwd - the same contract as `git -C`.
- The change lasts only for the command: the process cwd is restored when the command finishes.
- `SPECFLO_DIRECTORY` reaches everything that inherits the environment: the Claude Code SessionStart hook, the pi extension (its specflo calls and its statusline segment), and any subagent an orchestrating agent spawns. This is the intended effect for an agent driving a project in another tree. To make the redirect visible, `specflo status` prints a `Root: <path> (via -C|SPECFLO_DIRECTORY)` line (and `root` / `directory_source` keys in `--json`) when the directory was overridden, and the reseed payload from `specflo hook reseed` leads with one line naming the root when the env var is set. Without an override, all outputs are unchanged.

### Setup and orientation

- `specflo --version` - print the installed version and exit.
- `specflo guide [--json]` - orientation in one shot: what specflo is, the pipeline, the full command surface, and what to do next here. Runs **cold** (works before `specflo init`), so a fresh agent can get up to speed in any repo.
- `specflo init` - scaffold `.specflo/config.yaml` + the projects dir (default `docs/projects/`).

### Configuration

- `specflo config get <key>` - print one setting's resolved value, bare on stdout, so `$(specflo config get autonomy)` is the value itself. An unset key prints its shipped default.
- `specflo config list [--json]` - every setting with its resolved value, in registry order. A line ends with `(default)` when the file is silent about that key, or `(invalid, using default)` when the file's value is not one the key accepts. Keys specflo does not recognize are listed separately and left alone. `--json` reports each key's `value` and a `source` of `set`, `default`, or `invalid`.
- `specflo config set <key> <value> [--force]` - set one setting. The value is coerced to the key's type and validated **before** the write, so a rejected value leaves the file untouched and the error names what the key accepts. `active_project` is refused (use `specflo switch`), and `projects_dir` needs `--force` while projects live under the current path - changing it moves nothing, it only changes where specflo looks.
- `specflo config unset <key> [--force]` - drop one setting; it returns to the commented-out default line under its description, and reads as its shipped default again.

See **[The config file](#the-config-file)** for the file itself.

### Projects

- `specflo new <name> [--execution linear|fan-out] [--level quick|fast|full]` - create a project and make it active. `--execution` records the execution mode in `project.md` (default `linear`; see "Execution modes and fan-out" below). `--level` records how much ceremony the project gets (default `full`; see [Levels](#levels)); a quick project starts at `execute` with a `brief.md`. A level other than full is refused with `--remote`.
- `specflo level fast|full` - move the active project up a level, back to the brainstorm phase, keeping every document. Refuses the same or a lower level.
- `specflo execution linear|fan-out [--json]` - switch the active project's execution mode, in either direction, at any phase. Reports `unchanged` when the mode already matches; `--json` emits `{execution, changed}`.
- `specflo list [--json]` - list all projects, marking the active one and its phase.
- `specflo switch <name>` - make another project active (by slug or name).
- `specflo status [--json]` - show the active project, its phase, and what's next.
- `specflo shelve [<name>] [--reason ...]` - set a project aside: status `shelved`, phase untouched.
- `specflo resume [<name>]` - pick a shelved project back up at the phase where it was paused.
- `specflo leave [--json]` - clear the active-project pointer without changing any project. Nothing is written to the project; re-enter it later with `specflo switch <name>` (or `specflo resume <name>` if shelved). Idempotent: with no active project it prints `No active project.` and exits 0.

### Phase artifacts

- `specflo brainstorm start [--json]` - create (or locate) the active project's `brainstorm.md`.
- `specflo decision add --text ... [--rationale ...] [--supersedes D-NN]` - append a decision (`D-NN`) to the brainstorm.
- `specflo spec start [--json]` - create (or locate) the active project's `spec.md`.
- `specflo requirement add --text ... --acceptance ... [--from D-NN] [--supersedes REQ-NN]` - append a requirement (`REQ-NN`) to the spec.
- `specflo plan start [--json]` - create (or locate) the active project's `plan.md`.
- `specflo task add --text ... --acceptance ... --verify ... --from REQ-NN [--from REQ-NN ...] [--depends-on T-NN ...] [--files ...] [--needs <pool> ...] [--supersedes T-NN]` - append a task (`T-NN`) to the plan. `--from` (repeatable, required) links to the requirement(s) the task implements; `--depends-on` (repeatable) declares execution ordering; `--acceptance` is a behavioural pass/fail criterion; `--verify` is the command or step to confirm it. `--files` lists the files the task edits (comma-separated; one trailing parenthetical note per entry is allowed and stripped, as in `~/x/y (venv, outside repo)`); `--needs` (repeatable) names a resource pool the task needs - a non-empty token without commas or whitespace, such as `gpu:3090`.
- `specflo pool add <name> --size N [--json]` / `pool list [--json]` - declare a pool of `N` slots (`N >= 1`) in the CLI-owned `## Pools` section of `plan.md`, updating the size in place on a repeated add; `pool list` shows every declared pool plus every pool an active task needs. A pool nobody declared has one slot. `user` is a reserved pool name meaning the task runs in the main session with the user; it needs no declaration.
- `specflo plan graph [--json]` - render the plan's execution graph from its real data: waves by longest dependency path (wave 0 has no dependencies), one line per active task (id, title, progress, files, needs) and a mermaid `graph LR` block with one node per task, a subgraph per milestone and one edge per `Depends on` entry. `--json` emits `{waves, tasks, edges}`. Read-only: `plan.md` is byte-identical afterwards.
- `specflo milestone add --text ... --exit ... [--exit ...]` - append a milestone (`M-NN`) with its Exit checklist to the plan; `milestone list` and `milestone show` report rollup and the current milestone.
- `specflo doc show brainstorm|spec|plan|brief|checkpoint|project|followup|review-<N>` - print one artifact of the active project verbatim; `review-<N>` is a review round by its number. Agents read artifacts through this verb rather than opening files, so the same command serves a project in the checkout and one held by a daemon. An unknown name is refused with the valid names listed. `brief` is the quick project's brief, or at fast level a one-page view of the three documents; it is refused at full level.
- `specflo section set brainstorm|spec|plan|brief <section> --file <path>|--stdin` - replace one prose section's body (named with or without its `##`), keeping the header, every other section and every managed entry byte-identical and bumping `updated`. The managed sections (Decisions, Requirements, Tasks, Milestones, Pools) are refused with the verb that owns them.
- `specflo validate brainstorm|spec|plan|brief [--json]` - lint the phase's artifact and report readiness. `brief` checks a quick project's goal, its single check and its proof. The plan lint checks bidirectional REQ<->task coverage, that every task has acceptance + verification, and that dependencies resolve and are acyclic. It also warns (non-blocking) when two active tasks share a file with no direct or transitive `Depends on` edge between them, naming the pair and the path.

### Working the plan

- `specflo task start <T-NN>` / `task done <T-NN> [--note ...] [--closes <FU-NN> ...]` - mark a task `in_progress` / `done`. `task done --note` records the note in the same write as the state change. `task done --closes FU-NN`, repeatable, closes each follow-up the task settles with `--by <project>/<T-NN>` and the task's note, or else its title (`Task done.` for a blank title), as the close note, and `--json` lists them in `closed_followups`. Every named follow-up is checked open, and every followup document readable, before the task changes. `--closes` is refused for a project hosted on a daemon.
- `specflo task block <T-NN> [--reason ...]` / `task reopen <T-NN> [--note ...]` - mark a task `blocked` (optionally recording why) / return it to `pending` (optionally with a note). `task start` and `task block` take no `--note`.
- `specflo task edit <T-NN> [--title ...] [--acceptance ...] [--verify ...] [--scope ...] [--files ...] [--needs ...] [--implements REQ-NN[,REQ-MM]] [--add-depends-on T-NN ...] [--drop-depends-on T-NN ...] [--force] [--json]` - correct an active task's fields in place, rather than hand-editing `plan.md`. At least one edit flag is required; a field already holding the given value is reported as unchanged rather than rewritten. Every value is one line: a value carrying a line break is refused, and `--title`, `--acceptance`, `--verify` and `--implements` refuse an empty value (an empty `--scope`, `--files` or `--needs` clears that optional field). `--add-depends-on` and `--drop-depends-on` are repeatable and are refused before any write: an added edge when the named task does not exist, is superseded, is the task itself, or would close a dependency cycle; a dropped edge when the task does not hold it (so an edge onto a task that no longer exists can still be dropped). A **done** task refuses the edit unless `--force`, which rewrites the field and appends one `[Edit]` note per changed field carrying the value it overwrote; a **superseded** entry is frozen with or without `--force`. `--json` emits `{id, changed}`.
- `specflo task note <T-NN> --text ... [--label Note|Design|Resolution|Descoped] [--json]` - append one dated note line to a task entry: `- Note: <YYYY-MM-DD> [<Label>] <text>`. Notes accumulate at the bottom of the entry in the order written and work on a task in any progress state, including a done or superseded one. The label set is closed and `Edit` is reserved for `task edit --force`; the text is written as a single line (runs of whitespace and newlines collapse to single spaces) and empty text is refused. Notes surface in `specflo task show` and nowhere else - `task list`, `status` and `checkpoint` are unaffected - and a hand-written note that does not parse is a non-blocking plan warning, never a validation failure. `--json` emits `{id, note}`.
- `specflo task list [--json]` - all tasks with their progress state and the deps-aware next-actionable marker. `--json` is the orchestrator's frontier: each task also carries `files`, `needs` and `ready` (true exactly when the task is pending and next-actionable; an in-progress task is never ready, even when the next-actionable marker falls back to it because every pending task is held back), and the payload carries `pools`, a map from pool name to `{size, holders}` where holders are the in-progress tasks needing that pool.
- `specflo task show [<T-NN>] [--json]` - a task's brief: acceptance criterion, cited requirements, and constraints, plus the execution mode and, when set, `Files:` and `Needs:` lines (a task needing `user` is marked as not delegated). Defaults to the next actionable task.
- `specflo review start [--json]` - mint the next numbered review round (`review-N.md`) in the project directory and print its path. Numbering only ever goes up, so a deleted round leaves a permanent gap rather than a reused identity. With a round already open, prints that round's path and mints nothing - reusing it is how an abandoned review is resumed.
- `specflo review done --verdict ready-to-merge|changes-requested|waived [--reason ...] [--file <path>] [--json]` - close the open round by writing the verdict into its frontmatter, stamped with the date and, inside a git repo, the short `HEAD` sha. `waived` requires `--reason`, so a project that skipped review records why. `--file` ingests a reviewer's report as the round's body, refusing once that body has been written into. specflo never derives staleness from the stamp: commits landing after a closed round change nothing.
- `specflo validate execute [--json]` - completion gate: confirms every task is done, then that the latest review round closed `ready-to-merge` or `waived`. The gate keys on the verdict alone and never on the round's findings, so a passing round may still list nits.
- `specflo advance [--json]` - validate the current phase's artifact, then move the active project to the next phase (`brainstorm -> spec -> plan -> execute`).
- `specflo reopen [<phase>]` - the inverse of `advance`: move the phase pointer backward (bare `reopen` goes one phase back, `reopen <phase>` jumps to a named earlier phase). A pure pointer move; no artifact is rewritten.
- `specflo checkpoint [--json]` - print the active project's **resume prompt** (which phase, what to read, what to do next) and refresh `checkpoint.md`. The file is also rewritten automatically after every state-mutating command, so a freshly-cleared agent can jump back in with one command.

### Follow-ups

The work a project leaves for a later one. Each project keeps its entries in its own `followup.md`, created on the first add. The `FU-NN` numbers run across every project in the checkout, hand-written followup documents included, so an ID needs no project prefix. Only the followup verbs write the document: `section set` refuses it, and `doc show followup` prints it.

- `specflo followup add <title> --do <text> [--from <text>] [--json]` - add an open entry to the active project: a `### FU-NN - <title>` heading, a `- Do:` line, a `- From:` line when given, and `- Status: open`. Each value is one line, and an empty title or Do is refused. Two adds at the same time never get the same ID. Refused for a project hosted on a daemon.
- `specflo followup close <FU-NN> --note <text> [--by <ref>]` - close an open entry in any project of the checkout: its Status becomes `closed` and a `- Closed: <date>: <note>` line is added. `--by` names what did the work on a `- Closed by:` line below it: `<project>/<T-NN>` for a task in that project's plan, `<project>` for a project of the checkout, or a git commit, stored as its short SHA. A word that is both a project and a commit names the project. An unknown ID, an entry already closed, a hand-written entry, a missing or empty note, and a `--by` ref that names no project, task or commit are refused with nothing written.
- `specflo followup list [--all] [--json]` - the open entries of every project in ID order, with project, ID, title and Do line. `--all` adds the closed ones. Hand-written entries have no Status line and are not listed.
- `specflo followup show <FU-NN> [--json]` - one entry, open or closed, from any project of the checkout: project, ID, title and its Do, From, Status, Closed and Closed by lines (`closed_by` in `--json`, null when the close named no work). When two projects hold the ID, the open one is shown. An unknown ID or a hand-written entry is refused. `specflo guide` tells an agent to run it when the user names a follow-up to do.

`specflo advance` lists the open follow-ups of the project it completes (`followups` in `--json`), and `specflo new` prints how many open follow-ups the checkout holds.

### Session-start and unattended runs

- `specflo hook reseed [--format text|claude] [--continue]` - emit the **clear-and-continue** payload for the active project: a confirmation-gate directive (*do not start work; present the checkpoint and ask whether to continue*) followed by the verbatim checkpoint. Prints nothing for no active project, or when the active project is complete or shelved (nothing to resume, so the session starts silent). **Always exits 0, reads no stdin, makes no network calls** - safe to wire into a session-start hook unconditionally.
  - `--format claude` wraps the payload as Claude Code `SessionStart` JSON: the payload as `additionalContext` (re-grounds the agent) plus a user-visible `systemMessage` that tells you **what to type** to kick it off (Claude can't make the agent take a turn on its own). The default `--format text` stays portable plain text for any harness.
  - `--continue` swaps the confirmation gate for a direct *carry out the next step now* directive, and inlines the current task's brief - for a caller that cleared context on purpose and has already answered "keep going".
- `specflo hook install` - idempotently merge the `SessionStart` wiring into Claude Code's `.claude/settings.json`, preserving all existing content; a previously-installed (older) reseed entry is rewired in place rather than duplicated. The wiring calls `specflo hook reseed --format claude` on the `startup`, `clear`, and `resume` sources (`compact` excluded - its digest is retained).
- `specflo hook print` - print that same wiring as a JSON fragment on stdout (pipeable), either to merge into Claude Code's settings yourself or as a starting point to adapt for another harness (opencode, OpenAI Codex, ...); a stderr note marks it as a fragment and points at `specflo hook install` as the safe merge. pi needs no wiring - the bundled pi extension reseeds on its own (see **[The pi extension](#the-pi-extension)**). (`hook print --install` remains as a deprecated alias of `hook install`.)
- `specflo auto [--autonomy safe|autonomous|yolo] [--max-passes N] [--off|--on] [--ladder] [--json]` - emit the **auto-mode handoff payload**: an explicit, per-invocation opt-in that starts or continues an *unattended* run from the current phase toward project completion, emitting a bootstrap directive (autonomy policy + guardrail stop-conditions) instead of the ask-first pause. specflo only prints the payload - it drives no loop, spawns no nested agent, and never clears context; the clear-and-reseed trigger is the outer harness's job. Strictly additive - the default `hook reseed` / checkpoint behavior is unchanged.
  - `--autonomy` sets how far it runs unattended: `safe` (the default) and `autonomous` stop and hand off on any irreversible or outbound step; `yolo` permits them. Overrides the `.specflo` config default.
  - `--max-passes` is a runaway backstop: each invocation counts as one pass in a durable per-project run-state file, and on reaching the cap (default `50`) the run escalates to the human instead of continuing. Overrides the config default.
  - `--off` sets the durable kill switch (the next pass halts); `--on` clears it.
  - `--ladder` starts a [ladder run](#the-ladder-run) on a quick project; later passes continue it without the flag.
  - `--json` reports the pass as an object - its `payload` text, a boolean `stop`, and the `reason` that stopped it (`kill-switch`, `pass-cap`, `stall`, `project-complete`, `ladder-blocked`, or `unavailable`; `outgrew-level` is listed but no longer sent; `null` while the run continues) - so a machine caller reads loop control from the CLI instead of deciding it.

### Harness integration

- `specflo skills install|status|update|uninstall [--scope user|project] [--harness NAME[:SCOPE]]` - install specflo's bundled workflow skills into the agent harnesses on your machine, and keep them current. See **[Skills](#skills)**.
- `specflo extension install [--scope user|project]` - install the bundled pi extension into pi's extension directory: `~/.pi/agent/extensions/specflo` by default, `./.pi/extensions/specflo` with `--scope project`. A plain local copy with a version stamp - no npm, no network - and pi discovers the directory on its own, so no pi settings are read or written. Re-run to update. See **[The pi extension](#the-pi-extension)**.

### Hosted projects: the daemon and remotes

- `specflo serve --root <dir> [--bind <host>] [--port <port>]` - run the daemon on a root of its own, which holds a `projects` directory for the projects it hosts, a SQLite state store, its token hashes and its audit log. Binds `127.0.0.1:8741` unless told otherwise; `/health` answers without a token. Needs the `serve` extra (`pip install 'specflo[serve]'`).
- `specflo serve --root <dir> token add requester|developer|agent` - mint a bearer token bound to one of the daemon's three identities. The secret prints once; the daemon keeps only its hash. Every request but `/health` and the web UI's sign-in must carry a valid token, and every mutation records the identity behind it in `audit.jsonl`. The web sign-in page refuses an agent token.
- `specflo remote add <name> <url> --token <secret>` / `remote list` / `remote remove <name>` - register the daemons this checkout can reach. Each remote is one file under `.specflo/remotes/` holding its URL and token; `remote list` never prints tokens.
- `specflo new <name> --remote <name>` - create a project on a registered daemon. The checkout records which remote holds it and writes nothing else for it; every command then routes to the daemon for that project with no change in usage, and `list` marks it `[hosted: <remote>]`.
- `specflo promote <project> --remote <name>` - move a local project into a daemon: upload every file, verify the daemon's hashes against what was sent, and only then remove the local copy and record the project as hosted. A mismatch aborts with the local copy untouched.

See **[Hosting projects on a daemon](#hosting-projects-on-a-daemon)** for the model.

### The agent pool

These verbs need a daemon. `serve pool init` and `serve pool validate` work on the files under `--root`; the others run on the remote named by `--remote`, or on the only one registered, and are refused in a checkout with no remote. See **[The agent pool](#the-agent-pool)**.

- `specflo serve --root <dir> pool init` - write a starting pool directory under the daemon root: `pool/pool.yaml`, every line a comment, and the five shipped agent definitions in `pool/definitions/`. A file that exists is kept.
- `specflo serve --root <dir> pool validate` - check the whole pool directory without a daemon and print every fault in one run; exit 1 with any fault.
- `specflo serve --root <dir> pool reload [--remote <name>]` - ask the running daemon, as the developer, to read its pool directory again. A directory with a fault changes nothing, and neither does a change that removes a member with a lease out.
- `specflo lease request <pool> [--cwd <dir>] [--idle-limit <time>] [--label <text>] [--wait <seconds>] [--egress local|no-train|open] [--remote <name>] [--json]` - lease one member of a named pool; prints the lease id and the agent to drive with `specflo agent`, and keeps the lease token in `.specflo/leases/<agent>.token`. A full pool makes the request wait up to `--wait` seconds (600 by default; 0 refuses at once), with a notice on stderr.
- `specflo lease request --team <name> [...]` - lease every role member of a team, all or nothing, under one team lease id. Exactly one of `<pool>` and `--team`.
- `specflo lease release <lease> [--remote <name>] [--json]` - give a lease, or a whole team by its team lease id, back. A lease that has ended already is reported as it ended.
- `specflo lease list [--remote <name>] [--json]` - the leases this checkout holds tokens for.
- `specflo console attach <slot> <agent> [--remote <name>] [--json]` / `console detach <slot>` - attach your own running rpc agent on the daemon's host to a console slot the pool declares, or make the slot take no new lease. The developer identity only.
- `specflo egress local|no-train|open [--json]` - pin the active project's egress class, in any phase. A lease request made from a hosted project is never served by a member more open than the pin.

### Gates

- `specflo gate open <requester|developer> [--note <text>]` - hand the active project to a role: records the role waited on, the acting identity, the time and a one-line note of the open points. The project stays active. Opening while a gate is open is refused naming the open gate.
- `specflo gate take [--by <requester|developer>]` - close the open gate, recording who took it and when. `--by` names the human an agent relays the take for and is accepted from the agent identity only. Taking with no open gate is refused.

The gate fields are optional front matter on the project record: a record without them reads as before, and one with them survives shelve, resume, summary and advance. `project show` reports the gate, and the web UI reads it for the inbox and the take control (see **[The web UI](#the-web-ui)**).

### Products and work items

Products and their backlogs live in a daemon's state store, so these verbs take `--remote <name>`, or use the only remote registered.

- `specflo product add <name> [--slug <slug>] [--repo <location>]` / `product list` / `product show <slug>` - a product: a name, a slug derived from the name unless given, and an optional repository location.
- `specflo product set-vision <slug> [<text>|--stdin]` - replace the product's vision text.
- `specflo product piece add|list|remove <product> [<piece>]` - the deployable pieces a product is made of (web, admin, mobile, ...). A work item may target a declared piece; a piece a work item targets stays until the item is retargeted.
- `specflo product roadmap <slug> [--json]` - the vision, then the backlog in order with each item's status, kind, dev path, piece and spawned project. A view, never a write.
- `specflo workitem add <product> <title> [--kind <kind>] [--issue <link>] [--dev-path full|one-prompt|cyclical] [--piece <piece>]` - add a work item to a product's backlog. Kind is free text (`fix`, the default, `roadmap`, `idea` and `issue` are the usual ones); the dev path says how the item gets built and anything outside the three is refused.
- `specflo workitem list [--product <slug>] [--status open|in-progress|done|dropped] [--kind <kind>] [--json]` / `workitem show <id>` / `workitem set-status <id> <status>` - read and move the backlog.
- `specflo workitem spawn <id> [--name <name>]` - the one specflo project a `full` work item gets, created as a hosted project on the daemon that holds the item and made active in the checkout. The project records the item and the item records the project; a second spawn is refused.

## Execution modes and fan-out

Every project records an execution mode in `project.md` (`execution: linear`
or `execution: fan-out`; a file without the key reads as `linear`). Set it with
`specflo new --execution ...` or switch it later with `specflo execution ...`;
`status`, `checkpoint` and `task show` all state it.

- **linear** (default) - one agent works the plan one task at a time, as the
  execute skill has always done.
- **fan-out** - the main session is an orchestrator: it reads the frontier from
  `specflo task list --json`, runs `task start` and spawns one subagent per
  ready task with its `task show` brief, re-runs the task's verify step when
  the subagent returns, commits one task per commit and runs `task done`.
  Subagents never run `git` or `specflo`. Under fan-out the working-ahead note
  in `task show` is suppressed, since lanes crossing milestones is the
  expected shape.

The ready set (the `>` marker, `next_actionable`, `ready`) applies the same
rules in both modes. A pending task whose dependencies are done is ready
unless:

- an in-progress task shares a file with it (from the parsed `Files` lists), or
- a pool it needs has every slot held, counting one slot per in-progress task
  naming that pool (sizes from `pool add`; undeclared pools, `user` included,
  have one slot).

A slot or file frees when its holder is done, reopened or blocked; `task
reopen` is how an orchestrator releases work held by a dead agent. Tasks with
no `Files` and no `Needs` are never held back, so plans written before these
fields existed behave exactly as before.

The plan skill decomposes every plan to be fan-out capable regardless of mode:
every edited file in `Files`, an edge between tasks that share a file,
per-task outputs plus one merge task for parallel producers, contract-first
ordering, `--needs` plus `pool add` for hardware and shared environments, and
`--needs user` for user-in-the-loop tasks.

## pi subagents (`specflo agent`)

**Heavy work in progress.** The `specflo agent` surface is under active
development and changes constantly: commands, flags, the on-disk formats and
the behaviour itself can all move between releases, with no migration path.
Treat it as a preview to try, not a surface to build on.

The `specflo agent` group runs and controls headless pi coding agents. Each
agent is a detached host process that spawns `pi --mode rpc`, holds its stdio
as the sole owner, logs every event, and exposes a per-agent Unix socket for
control. A controller (you, or an orchestrating agent) drives it entirely
through the CLI; the host never interprets assistant text, so conventions like
completion phrases stay controller-side.
An rpc agent answers at its host's socket only: the host starts pi with
`SPECFLO_AGENT_SERVE=0`, so the specflo pi extension inside that pi serves no
control socket of its own.

### Verbs

- `specflo agent start <name> [--transport rpc|tui] [--cwd DIR] [--pi-cmd CMD] [--workspace ID] [--no-herdr] [--no-auto-answer]` -
  start an agent in `DIR`. The default transport `rpc` starts a detached host
  that spawns pi headless; both survive the invoking shell. `--transport tui`
  runs a real interactive pi in a herdr pane instead (see The TUI transport
  below); an unknown transport value is rejected naming the valid ones. Names
  are unique among live agents; a name is reusable once its host is stopped
  or dead. `--pi-cmd` overrides the spawned command (default `pi --mode rpc`,
  or plain `pi` under `--transport tui`; add `--model provider/id[:thinking]`
  there to pick a model). `--no-auto-answer` disables the dialog policy so a
  controller answers dialogs itself (rpc transport only).
- `specflo agent status <name> [--json]` - live-checked status: the socket is
  probed first, so a dead host reports `dead` instead of echoing stale state.
  `--json` carries the full snapshot (name, state, host and pi pids, context
  percent when known, herdr workspace/tab/pane ids, last activity) plus the
  state-dir paths (socket, events.jsonl, status.json).
- `specflo agent list [--json]` - every known agent with its live-checked
  state, each row carrying transport (`rpc`/`tui`) and ownership
  (`managed`/`adopted`). A dead session is never shown as attachable, and a
  cleanly exited session's leftover event log is not listed at all.
- `specflo agent prompt <name> "<text>" [--timeout S] [--no-wait] [--steer | --follow-up]` -
  send a prompt; block until the run settles and print the final assistant
  text to stdout. A working agent refuses a plain prompt (exit 10) unless
  `--steer` or `--follow-up` delivers it with pi streamingBehavior `steer` or
  `followUp`. `--no-wait` submits and returns immediately.
- `specflo agent wait <name> [--timeout S]` - block until the current run
  settles; exits 0 immediately when nothing is in flight.
- `specflo agent last <name>` - print the most recent final assistant text.
- `specflo agent reset <name>` - clear the agent's conversation context in
  place (pi `new_session`). The pi process, its working directory, its model
  and the prompt it was started with stay. A working agent refuses it (exit
  10).
- `specflo agent log <name> [--follow]` - print the agent's event log
  (events.jsonl); `--follow` streams new events as they land. For the
  holder of a lease it begins where the lease was bound.
- `specflo agent stop <name> [--timeout S]` - stop follows ownership. An rpc
  agent gets the v1 graceful stop: abort any in-flight run, terminate pi then
  the host (escalating to kill after a bounded grace period), release the
  herdr registration, and write the final `stopped` state with events.jsonl
  and status.json retained. A managed tui agent gets SIGTERM to the recorded
  pi pid; the extension's own shutdown removes the socket and record. An
  adopted session is never killed: stop detaches - it removes specflo's
  record, leaves pi running, says so, and exits `13`.

`status`, `prompt`, `wait`, `last`, `reset`, `log` and `stop` take
`--lease-token` for an agent leased from a daemon's pool; see
**[Leases](#leases)**. An agent under no lease needs none.

### Exit codes

Consistent across every agent verb, and stated in each verb's `--help`:

| Code | Meaning |
|------|---------|
| 0    | success / run settled |
| 10   | agent busy (working; use `--steer` or `--follow-up`) |
| 11   | wait timeout |
| 12   | host unreachable or unknown agent |
| 13   | adopted session detached (`stop`; pi left running) |
| 1    | generic usage or error |

### The TUI transport (`--transport tui`)

`start --transport tui` runs a real interactive pi - the TUI a person sits
at - in a herdr pane, and makes it a controllable agent at the same time. No
host process exists on this path: the specflo pi extension inside the session
binds the same per-agent control socket the v1 host binds, writes the same
discovery record, mirrors run events to `events.jsonl`, and drives
`status.json` through the same lifecycle, so every verb above (and the
Python client under them) works identically against both transports. herdr
is required for a tui start - the pane is the transport - and the command
reports success only once the socket accepts a connection; on timeout
(`SPECFLO_AGENT_START_TIMEOUT` overrides the bound) the pane is closed and
the exit code is `11`.

A controller and a human share the one session: socket prompts and typed
input land in the same transcript, in order. A plain socket prompt against a
streaming session is refused (exit 10); `--steer` and `--follow-up` deliver
with pi's streaming behaviors. For a managed pane the extension pushes real
lifecycle state into herdr - working during a run, idle at settle, blocked
while a UI prompt is open - and a blocking dialog also flips `status.json`
to `needs-attention` with the prompt kind and title in the event log, so a
controller can see the session is waiting and answer the dialog with pane
keystrokes (`herdr pane send-keys <pane> ...`).

Serving is on for every pi session with the extension loaded, so a session
someone started by hand is discoverable and adoptable: it appears in `list`
as `tui adopted`, named by its cwd basename (pid-suffixed on collision), and
can be attached with `status`, `prompt`, `wait`, `last`, and `log` like any
agent. Disable serving entirely with `SPECFLO_AGENT_SERVE=0` (or `off`), or
a `serve.off` marker file in the state base dir. On clean pi exit the socket
and record are removed and the event log is retained; a record left by an
unclean death is probed, never trusted - `list` marks it dead, and a new
session for the same identity replaces it.

### herdr placement and degraded mode

When the `herdr` CLI is installed and its server answers, `start` creates or
reuses a workspace labeled by the `agent_space` config key (default `agents`),
creates one tab labeled with the agent name, and runs the host inside that
tab's pane. The pane shows a live human-readable transcript - submitted
prompts, streamed assistant text, tool executions, dialog answers, and state
changes, never raw protocol frames - and accepts no input. The host pushes
real agent state into herdr (`herdr pane report-agent`) across the lifecycle,
so `herdr agent list` mirrors it without heuristic detection, and releases the
registration on stop. `start --workspace <id>` places the tab in the given
workspace instead of the `agent_space` one.

When herdr is unavailable (not installed, or the server is down) or
`--no-herdr` is passed, `start` proceeds headless with exactly one warning on
stderr naming the degradation; everything else works the same, with the
transcript going to `host.log` in the agent's state dir.

Per-agent state lives under `~/.specflo/agents/<name>/` (override with
`SPECFLO_AGENT_STATE_DIR`): the control socket, an append-only timestamped
`events.jsonl`, and an atomically updated `status.json` snapshot.

Dialogs from pi extensions are auto-answered by policy: confirm affirmed,
select gets the first policy-safe option, input/editor get a policy nudge, and
a dialog matching the danger pattern is cancelled. Every dialog and answer is
logged; past a per-run flood threshold the agent flips to `needs-attention`
and auto-answering stops for that run.

## Hosting projects on a daemon

**Heavy work in progress.** Daemon hosting, and the web UI with it, is under active development and changes constantly: commands, flags, the on-disk formats and the behaviour itself can all move between releases, with no migration path. Treat it as a preview to try, not a surface to build on.

A project lives in one place: in a checkout under the projects directory, or on a daemon. `specflo serve` runs the daemon on a root of its own; a checkout registers it with `specflo remote add` and then creates projects there with `new --remote`, moves existing ones there with `promote`, and works them with the same commands as before. The daemon holds the only copy of a hosted project's artifacts; the checkout keeps a pointer and nothing else.

The daemon knows three identities, `requester`, `developer` and `agent`, each with its own bearer tokens minted by `serve token add`. Every request carries one, and every mutation is recorded with the identity behind it. The two human identities sign in to the web UI; the agent identity is what a daemon-started agent acts as, and the sign-in page refuses it. There is no permission system beyond that: the identities exist so the handoff between requester and developer is recorded.

The daemon also holds products and their work items, the layer above projects: a product has a vision, declares the pieces it is made of, and carries a backlog; a `full` work item spawns exactly one project, cross-linked both ways. `product roadmap` reads the vision and the backlog back in order.

### The web UI

With the `serve` extra installed the daemon serves a web UI beside its API. A browser signs in at `/signin` as `requester` or `developer` with that identity's token and receives a session cookie; the token never reaches the browser and the cookie never unlocks the API. Without a session every page redirects to sign-in. Sessions live in the daemon process, so a restart signs every browser out.

- `/` - the signed-in identity's inbox: every hosted project with an open gate for their role, newest first, with its product, name, note and opened time. Below it, every product with the first line of its vision, its open work items and its active projects.
- `/products/<slug>` - a product's vision, pieces, backlog and projects; complete projects are hidden until the archived filter (`?archived=1`) is on. A work item whose dev path is `full` and has no project carries a start-project control.
- `/projects/<slug>` - a project's phase, status, execution mode, the role it waits on (from the open gate with its note, opener and time when there is one, from the phase otherwise), each artifact with the same text `doc show` prints, and, in the brainstorm phase, the agent section: the agent's state, its transport, and the chat.
- `/pool` - the daemon's agent pool, and for the developer the pages under it that manage agent definitions and teams. See **[The pool in the web UI](#the-pool-in-the-web-ui)**.

The UI has exactly nine controls that change state, and a structural test pins the set: start project on a work item, send a message on a project, take a gate on a project, start agent on a project, and on the pool pages release a lease, save and delete a definition, and save and delete a team. No control advances a phase, runs auto mode or edits an artifact; that stays with the CLI. Every form carries the session secret as a hidden field the route checks.

The pages are server-rendered Jinja2 templates shipped in the package. The browser scripts, htmx 4 and its sse extension, are vendored in the package's `assets` directory; there is no JavaScript build step.

### The daemon seat

Starting a project from its work item is one daemon operation, also callable without a browser: spawn the hosted project, scaffold its seat, start its agent, and record the project-to-agent mapping once the agent's control socket answers. The seat is a client checkout under the daemon root's `seats` directory, named for the project: a config whose active project is the hosted one, a remote entry pointing at the daemon's own URL with an agent token, and no artifacts. The agent is a pi started with `specflo agent start` in that seat, named for the project, over the transport the `agent_transport` config key names: `tui` (the default) places a real pi in a herdr pane, `rpc` a headless host. A start whose socket never answers within the timeout fails with the project and seat kept, and the project page then offers a start-agent control.

The fresh agent gets an opening prompt naming its seat as the requester and pointing it at the brainstorm skill's requester mode: plain language, what and why rather than how, one early landscape scan presented plainly, decisions recorded in plain words, and the gate opened with a note of the open points when the requester says they are done. The open gate shows in the developer's inbox. When the developer takes it, from the take control on the project page or by their word in the pane (the agent relays it with `gate take --by developer`), the daemon tells the agent the developer seat is in the conversation and the skill goes back to its normal process.

The chat is shared by the web and the pane. A message posted from the project page reaches the agent as a prompt prefixed with the poster's label; an agent mid-run takes it as a steer, and the post returns once the agent has the prompt. The daemon holds one subscription to each live agent's socket and appends every user message from any seat, every assistant message and every state change to a durable per-project chat log under its root, each entry with a monotonic id. The page follows the log over one server-sent-events stream with replay from the last id it saw, shows the agent's state (working, idle) and a needs-attention banner while a blocking dialog is open in the session, which says to answer in the pane. The log and the mapping survive a daemon restart: the daemon finds the live agent again without a new start. Advancing the project out of brainstorm stops the agent and clears the mapping; `specflo agent stop` does the same by hand.

## The agent pool

**Heavy work in progress.** The agent pool, with its leases, teams and consoles, is under active development and changes constantly: commands, flags, the on-disk formats and the behaviour itself can all move between releases, with no migration path. Treat it as a preview to try, not a surface to build on.

A daemon can hold an agent pool: a roster of pi agents that an admin declares ahead of time and that orchestrators in any checkout lease by name, use through the `specflo agent` verbs, and give back. Every pool on the daemon draws on one ledger, so two projects cannot both claim the same member, provider account slot or GPU room. The pool is a daemon feature. Local mode is unchanged: a checkout with no registered remote loads no pool code, and the `lease` and `console` verbs are refused there, naming `specflo remote add`.

```bash
specflo serve --root ~/specflo-daemon pool init       # a commented pool.yaml and the shipped definitions
# edit ~/specflo-daemon/pool/pool.yaml: declare members and pools
specflo serve --root ~/specflo-daemon pool validate
specflo serve --root ~/specflo-daemon                 # a daemon that runs already: ... pool reload

# from a checkout that registered the daemon
specflo lease request workers --idle-limit 30m        # prints the lease id and the agent name
specflo agent prompt coder-local "Run the tests and report what fails."
specflo lease release <lease>
```

### The pool directory

The configuration is the directory `pool/` under the daemon root, which an admin edits by hand:

- `pool/pool.yaml` - five sections, and a key it does not know is refused:
  - `llama_swap` - the path of the rig's llama-swap configuration file; a relative path is taken from the pool directory. A local member needs it.
  - `models_file` - the path of the operator's pi `models.json`; a relative path is taken from the pool directory. A local member needs it: its generated `models.json` is the operator's provider from this file, filtered to the one model the member declares.
  - `accounts` - provider (OpenRouter) accounts: `name`, `cap` (the most leases that run through the account at once) and `key_env` (the name of the environment variable that holds its API key, never the key). There is no field for a management key, and the pool creates no keys.
  - `members` - the whole roster; nothing that is not declared can be leased. A member has `name`, `command` (the harness command that starts it, for example `pi --mode rpc --provider llama-swap --model <id>`), `backing` (`local` or `hosted`), `model` (a local member: one llama-swap model ID or one of its aliases) or `account` (a hosted member: a declared account), `labels`, `capacity` (how many leases it serves at once) and `egress` (`local` for a local member, `no-train` or `open` for a hosted one). `kind: console` declares a console slot instead (see **Consoles**): no `command`, capacity 1.
  - `pools` - the named pools a request asks for: `name`, `definition`, `members`, `size` (the most leases the pool grants at once), `idle_default`, `idle_max` and an optional `preempt_after`. The three times are a whole number and a unit, `s`, `m` or `h` (`10m`, `4h`).
- `pool/definitions/<name>.md` - an agent definition: what a role is, not what runs it. The YAML front matter carries `role`, `tools`, `skills`, `deny` (commands the member must not run), `env` and `credentials` (the names of the environment variables it may be given: a `credentials` entry is a variable name, such as `GITHUB_TOKEN`, not a label for a secret), `needs` (the labels a member must have), `egress` (the most open class it accepts), `project_context` and `paths` (files and directories its members get back read-only inside the sandbox; see **Egress classes and closed accounts**); the body is the system prompt. Five ship with the package: `worker`, `critic`, `hermes-rebaser`, `model-update-checker` and `landscape-scanner`.
- `pool/teams/<name>.md` - a team: front matter `roles`, each a `name`, a `pool` and a `count`; the body is free notes. `pool init` makes no `teams` directory.

`serve pool init` writes `pool.yaml` with every line commented out, so it declares nothing, and copies the five definitions; a file that exists is kept. `serve pool validate` checks the whole directory without a daemon and prints every fault in one run, each naming the file, the entry and the field; it exits 1 with any fault. Whether a member meets its pool's definition (every label it needs, a class no more open than it accepts) is settled there and never while a request waits. A `no-train` hosted member on an `anthropic/` model is refused, because pi does not send the provider routing flags for those models. A hosted member that declares a `model` must select it with `--model` in its `command`: its generated `models.json` pins no model, so without the flag it would run the provider's default. A `pool.yaml` or llama-swap file that is not a regular file, such as a named pipe, is refused before it is read.

The daemon reads the directory when it starts. A configuration with a fault disables only the pool: projects are served as before, every pool route answers 400 with the full list of faults, and the pool page shows them. `serve pool reload` asks a running daemon to read the directory again with no restart, leases kept; it reaches the daemon through a remote registered in the checkout with the developer's token. A reload with a fault changes nothing, and so does one that removes a member with a lease out or changes that member's kind. A reload that passes also brings to life a pool that was invalid at start.

Each lease on a started member runs a fresh pi, started through `specflo agent start` on the rpc transport in a herdr pane named for the member and stopped when the lease ends. The definition reaches pi as `--tools` (or `--no-tools`), `--append-system-prompt` and, unless `project_context` is true, `--no-context-files`; its skills are copied into the member's generated configuration directory, where pi finds them by name. The member's environment is built from nothing: a short baseline (`PATH`, `HOME`, locale and terminal variables), what the definition lists, and the key of the member's own account; the prompt and the key never sit on a command line. Every member gets a generated pi configuration directory in place of `~/.pi/agent`, and for a `no-train` member its `models.json` carries the OpenRouter routing flags `data_collection: deny` and `zdr: true`.

### Leases

- `specflo lease request <pool>` asks for one member of a named pool and prints the lease id and the agent name. The member starts in the directory the request was made from, or in `--cwd`; it must be a directory on the daemon's host. A directory that would bring back what the sandbox hides is refused: `/`, the home or a directory above it, a hidden directory or a directory above or inside one, and a directory inside `.specflo`. A request from the home directory itself is refused, so run it from a checkout or pass `--cwd`. `--label` is what the pool shows as the holder. A pool the configuration does not declare is refused.
- The lease token is written to `.specflo/leases/<agent>.token` under the checkout (mode 0600, in a directory that ignores itself) and is never printed. Every `specflo agent` verb finds it upward from the working directory, or takes `--lease-token` or `SPECFLO_LEASE_TOKEN`. While a lease is out the member's host answers only its holder: a verb with no token or another token exits 1 with `agent '<name>' is leased to another holder` and shows nothing of the member, and the host sends its events only to connections that presented the holder's token or the pool's.
- A request that does not fit waits on the daemon, up to `--wait` seconds (600 by default). It says so at once on stderr: the pool, what is full, its place among the waiting requests and the limit; with `--json` the notice is one JSON object on stderr and stdout carries only the result. `--wait 0` refuses a full pool at once, and interrupting the command cancels the request. Waiting requests are served in arrival order: a grant gives way to every earlier request that fits now, and a request that does not fit holds up no one behind it.
- A lease has an idle limit: `--idle-limit`, which the pool's `idle_max` must allow, or the pool's `idle_default`. There is no renew verb. Every verb the holder runs on the member and every turn the member works renews the lease, and a member in a turn counts as active however long the turn is. A lease idle for its limit expires. Nothing runs in the background: the daemon ends expired leases at the start of the next pool request or pool page, whoever makes it.
- `specflo agent reset <agent>` clears the member's conversation (pi `new_session`) on the same lease; the process, its directory and its model stay.
- `specflo lease release <lease>` gives the lease back and removes the token file; a lease that has ended already is reported as it ended. `specflo lease list` prints the leases the checkout holds tokens for, and never another orchestrator's.
- After a lease ends, the former holder's next agent verb exits 12 and stderr names how: `lease released`, `lease expired` or `lease preempted by <request id>`. The member and its context are gone.

### Egress classes and closed accounts

A member's egress class says where its prompts go: `local` (llama-swap on the daemon's host), `no-train` (a hosted provider that is told to collect and retain nothing) or `open` (any hosted provider). A request is served only by a member no more open than its ceiling, which is the strictest of three: the `--egress` of the request (`no-train` when none is given), the class the pool's definition accepts, and the class the requesting project pins. `specflo egress <class>` pins the active project; the client sends only the project's slug and the daemon reads the pin from its own record, so nothing in a request widens it. There is no unpin verb. A pool with no member under the ceiling is refused at once and never waited on.

Every member the pool starts runs inside a bubblewrap sandbox, and its egress class picks the sandbox's network. The filesystem is the same for every class: a read-only root, a fresh `/tmp` and `/run`, and a tmpfs or an empty file over the operator's home, `~/.pi`, `~/.agents`, `~/.specflo`, the directory `PI_CODING_AGENT_DIR` names, the runtime directory (`XDG_RUNTIME_DIR`) and the daemon root. It also hides the `.specflo/leases` and `.specflo/remotes` directories of every checkout from the member's working directory up, which hold lease tokens and daemon tokens. The fresh `/run` keeps a member from the sockets the host's daemons listen on there, such as the container engine's and the system bus, which trust the operator's user. A member that shares the host's network gets back the one file there that name resolution needs, the target of `/etc/resolv.conf`. What a member gets back is its harness's installation, the pool's deny-list extension, its working directory and the pi configuration directory generated for its lease. The generated directory carries a copy of the `fd` and `rg` that the operator's pi keeps in its `bin`, when it has them, so a member's search tools run without a download. A member's `TMPDIR` is `/tmp`, whatever the daemon's is. The working directory and the generated directory are bound at their real paths, so a working directory given through a symlink is bound where it really is. A member whose harness is installed at or above a hidden path, such as a `pi` in `~/bin`, is refused at the start, naming the program: install the harness under a directory below the home.

A definition's `paths` bring back what its role needs from under the swept home, read-only: a tool installed there, or data it reads. A leading `~` is the home. Each path is bound at its real path. When the path itself is a link, that link and each link it leads through that lies in a swept directory are made again, so a program on `PATH` that is a link into an environment of its own runs by its name. A link in a directory part of the path is not made again, so list such a path by its real path. A uv tool is that shape, and needs both listed:

```yaml
paths: [~/.local/bin/tvly, ~/.local/share/uv/tools/tavily-cli]
```

A uv tool's environment runs on an interpreter uv manages under `~/.local/share/uv/python`, which is swept too; list the interpreter's directory as well, or install the tool on a system Python. A path that is missing, that is at or above a hidden path, inside a hidden path other than the home, in or directly holding a `.specflo` directory, in the fresh `/tmp` or `/run` and not in the home, in the sandbox's own `/dev` or `/proc`, or a link to any of those is refused by `pool validate` and again at the member's start. So a listed path cannot bring back a host socket such as the container engine's or the display's, the host's shared memory in `/dev/shm`, or the host's process list. A checkout below a listed directory keeps its `.specflo/leases` and `.specflo/remotes` hidden when specflo has recorded it in `~/.specflo/checkouts` before the member starts: `specflo init`, storing a lease token, adding a remote and any specflo command run inside a checkout record it. At each start the pool makes and hides the token directories of every recorded checkout below a listed path, so a token written there during the lease stays hidden too. When the daemon reads the pool directory, at its start and at `serve pool reload`, it records where each listed path leads: each link on the way and the real path. A member start whose listed path leads elsewhere by then is refused, since a member that can write the path could have pointed it at any path under the home. Reload the pool to accept a change that is meant. The shipped definitions list none.

Every member's git has the operator's identity and nothing else of the operator's configuration: the generated directory holds a git configuration file with the `user.name` and `user.email` the daemon's git reports outside any repository, and `GIT_CONFIG_GLOBAL` names it. A member commits and rebases under that name, and a repository with an identity of its own keeps it. With no identity to give, no file is written and the variable is not set; a definition cannot set it.

When the user manager can be reached, each member runs in a systemd scope of its own (`systemd-run --user --scope`) with a task limit of 512, and that is its process limit: it counts only what the member runs, so host load cannot starve a member and one member's fork loop stops at its own limit. The scope runs the member in place, so its pid, pipes and pane are as before, and it is gone when the lease ends. Each scope is a unit named `specflo-member-<agent>-<id>`, so `systemctl --user list-units 'specflo-member-*'` shows the members that run. A member killed while its scope is being made can leave the scope behind with nothing in it; the expiry pass stops every such empty member scope. The daemon checks for the user manager at each start. Without one, as for a daemon started outside a login session, a member starts under the per-uid process limit, which counts every thread the operator runs, with a margin of 1024 above the count at its start, plus the 16 processes the sandbox itself forks; the daemon logs the reason once. Without linger the user manager, and every member's scope with it, stops when the operator logs out: run `loginctl enable-linger` if members must outlive a logout.

The sandbox keeps a member from reaching the operator's secrets and other members' state by ordinary means: reading a path, listing a directory, running a tool. It is not a jail against a determined process that runs as the operator's user. The Known limits below name what it leaves open.

- `local`: the member has a network namespace of its own whose one interface is loopback. It reaches llama-swap through the daemon's bridge: a unix socket the daemon serves in its root (`llama-swap.sock`, mode 0600), bound into the sandbox, with a `socat` forwarder inside that listens on the port of the member's provider base URL before pi starts. The daemon's side answers chat completions, completions and the models listing and refuses the rest of llama-swap with 403, also to a member that stops the forwarder and speaks to the socket itself. A completion is forwarded only when it names a model that an active lease or a project agent holds, by its llama-swap ID or an alias, so a member cannot make llama-swap load a model the ledger did not admit and evict another member's; any other is refused with 403. A member can still name a model another lease holds, which loads nothing and shares that model's turns. `local` is the one class for which specflo claims that nothing the member reads or writes leaves the host.
- `no-train` and `open`: the member shares the host's network, because its prompts go to its provider. Its own tools reach the internet as well: a bash command, a fetch or an install run by the member can send anything the member can read. The class says which provider may see the prompts; it says nothing about what the member's tools send.

A local member's provider base URL must be plain HTTP on `127.0.0.1` or `localhost`. Its start is refused when it is not, when `socat` is not on the daemon's `PATH`, and when the daemon does not serve the bridge. The daemon serves the bridge while its root has a pool directory, from the moment it starts; a second daemon started on the same root does not start.

While it serves, the daemon reads the key of every declared account (`GET /api/v1/key`, the one provider call it makes): once when it starts and every 15 minutes after that. The figures show on `/pool`. An account with no free-model requests left for the day is closed until the next midnight UTC. A read that fails shows on `/pool` as the read error and closes nothing.

The provider can refuse a hosted member's call with a 402. When the key's credit limit or the account's credits are used up, the daemon reads the key again and closes the account until the key's limit resets, or until the next midnight UTC when the read says nothing of a reset. A closed account ends no lease, but its members take no new one: a request that only closed accounts could serve is refused at once, naming each account and its reopen time. When the 402 is the in-flight spending budget, the account stays open and the daemon sends the member the same prompt again. A 429 is left to pi, which retries it.

### The ledger

One ledger admits every request. It counts the active leases against each pool's `size`, each member's `capacity` and each account's `cap`, across pools, so a member or an account that two pools share is counted once; nobody has a reserved share. A member of capacity above 1 runs each further lease under its name with a number (`m1.2`, `m1.3`). A local member also takes its model: it fits only when llama-swap's configuration (the sets of its `matrix`) lets that model stay loaded beside every model under lease. The pool only reads that file and llama-swap's `GET /api/events`, from which the pool page counts model reloads of leased members; it never asks llama-swap to load, unload or place a model, and a request that does not fit waits for a lease to end.

The agents the daemon runs for hosted projects are no members, but they use the same rig and accounts. While one is alive it is a standing entry in the ledger with the model or account slot that two keys of the daemon root's `.specflo/config.yaml` name, `project_agent_model` and `project_agent_account`. A standing entry takes no pool slot, has no idle limit and is never ended by the pool.

### Teams

`specflo lease request --team <name>` takes no `<pool>`. A team is granted all or nothing: every member of every role must fit at once, a team that waits holds nothing, and one that would not fit an empty pool is refused at once. The grant prints one team lease id and, for each role member, an agent with a token file of its own. The team is kept as one: every member lease has the same idle limit (the shortest `idle_default` of its pools, or the asked value, which the shortest `idle_max` must allow), activity on any member renews all of them, and they expire together. `lease release <team lease id>` ends every member; a release of one member lease is refused naming the team lease id. The members have no way to message each other; the orchestrator relays.

### Preemption

Preemption is opt-in per pool. A lease of a pool that declares `preempt_after` may be taken once it has been idle for longer than that, never while its member is in a turn, and only by a request that already waits; a lease of a pool that declares none is never taken. There are no priorities. The fewest leases that make the waiting request fit are ended, the longest idle first, and a team is taken whole or not at all. A request does not take what would serve a request that arrived before it.

### Consoles

A member of `kind: console` is a slot for a developer's own running agent. `specflo console attach <slot> <agent>` binds an agent that runs on the daemon's host under `specflo agent start` on the rpc transport; a TUI agent is refused, and both verbs are served to the developer identity only. While attached, the slot is matched, counted and leased like any member, and the lease runs on the attached agent. A lease on a console starts nothing, stops nothing and does not apply the pool's definition. At each grant the pool clears the pi's conversation (pi `new_session`, same process), so a holder gets nothing of a former holder's turns nor of the developer's own: attaching a console gives its conversation up. It goes both ways: the host is the developer's, so once the lease has ended the developer's own `specflo agent log` reads the turns the holder took on it. Lease out a console only to a holder whose work you may read, and take a lease on one only if the slot's owner may read yours. A console whose developer has a turn running, or whose pi has exited, takes no lease while it is in that state: the placement passes the slot over, so the request takes the next member of the pool or waits, as for a slot with no agent attached, and the developer's turn is left alone. The slot reads attached all the same. A console whose pi does not clear its conversation (an extension that cancels the new session) is not leased, and the request is refused naming it. An agent whose name a configuration starts a member's host under is refused at attach, and a reload that declares such a member while the agent's attachment stands is refused. When the lease ends the pool lowers the holder wall, aborts a turn the holder left running, and leaves the process alone. Between leases the host refuses the tokens of former holders. `specflo console detach <slot>` makes the slot take no new lease; a lease that is out stands. A slot with no agent attached, or whose host is gone, is offline, and a request that only such slots could serve waits.

### The pool in the web UI

`/pool` shows, to the requester and the developer alike: each pool with its size, leases in use, waiting requests and limits; the waiting requests; each member with its kind, state, backing, model or account, class and model reloads; each lease with its holder label, team lease id, idle time and time to expiry; each account's figures; the standing entries; and the latest lease transitions. It shows nothing a member wrote (no prompt, reply or log) and no token. With an invalid configuration it lists the faults and nothing else.

The developer's page has a release control on every active lease, which ends the lease, or the whole team of a member lease, as `released by developer`; the former holder is told `lease released`. The developer also has pages that list, create, edit and delete agent definitions (`/pool/definitions`) and teams (`/pool/teams`). A save writes the same file an admin edits by hand, and only when the whole directory passes `pool validate` with the new file in place; otherwise the faults show beside the form and nothing is written. After a save the daemon reloads the pool. Deleting a definition that a pool binds is refused naming the pool. Every one of these posts checks the session secret and is recorded in `audit.jsonl` with the acting identity. Accounts, members and named pools have no edit pages, and no page runs an agent.

### Plan `Needs` lines on a daemon

For a hosted project, a task's `Needs` name that is one of the daemon's pools is counted by the daemon: the pool's `size`, and as many slots taken as the pool has leases out, whichever project holds them; a lease past its idle limit is not counted. The ready set of `task list` uses that count in place of the plan's own; other names keep the plan's `pool add` count. `validate plan` on a hosted project reports a `Needs` name that is neither a daemon pool nor declared in the plan. A local project reads no pool. The execute skill's fan-out step tells the orchestrator to request a lease before such a task and release it after, on success or failure.

### Known limits

- Inside its sandbox a member's pi runs as the daemon's user with the tools its definition lists. A hosted member's tools share the host's network, so nothing but its class's provider choice limits what they send.
- A daemon whose root gets a pool directory after it started serves no bridge, and starts no local member, until it is started again.
- The deny list is a guard against mistakes, not a security boundary. It matches the text of a bash command, so `git push` in the list stops `git push origin main` and does not stop `git -C repo push`, an alias or a script. A list that cannot be read blocks every bash call.
- Stock pi does not carry a provider 402's `Retry-After`, so an in-flight budget refusal is sent again after a fixed 5 s, with no cap on the number of tries.
- pi sends the `no-train` routing flags only from its OpenAI-completions client. Validation refuses the known case (`anthropic/` models), and a hosted member that declares a model must select it with `--model`; a hosted member that names its model in its command alone is checked by that name.
- Token files do not record their remote, so with several remotes `lease list` and `lease release` present every stored token to the remote they run on.
- A definition's `credentials` entries are environment variable names, treated like `env`.
- A local member's `model` is a llama-swap model ID or one of its aliases, not a profile or a selector.
- The pool never loads a model, so a holder's first prompt pays the model's load time; give that prompt a `--timeout` that allows for it.
- The llama-swap address comes from `SPECFLO_LLAMA_SWAP_URL` in the daemon's environment (default `http://127.0.0.1:8080`), not from `pool.yaml`.
- `project_agent_model` and `project_agent_account` are written by hand; no verb sets them and `pool validate` does not check them.
- `status`, `checkpoint` and the web project pages do not read daemon pool capacity; only `task list` and `validate plan` do.
- A request from a checkout whose active project is local, or that has none, carries no project and gets no pin.
- A console host remembers the lease tokens it has cleared in memory only, so a host that was started again accepts a former holder's token until its next lease.
- A host keeps the 16 newest ended records per member; an older former holder gets the plain refusal, exit 1.
- A member's `events.jsonl` is a plain file that any process of the same user can read.
- A free provider model is not a dependable member. A free model sits on a shared upstream pool and is rate-limited whole days at a time; pi retries and gives up, and the daemon's account read counts credit (`usage`, `limit`), not the provider's free-request budget, which no page shows. Give a member that must answer a cheap paid model.
- The sandbox hides the token directories of the checkouts at and above the member's working directory only. A member can read the lease and daemon tokens of a checkout below its working directory, and of any other checkout outside the home.
- A checkout below a listed directory that is not in `~/.specflo/checkouts` when a member of that definition starts, such as one cloned or made while the member runs, is not hidden from that member, and it reads the lease and daemon tokens stored there. Run any specflo command inside a checkout to record it. The next member start hides it.
- A listed directory brings back everything under it that is not in the hidden set this section names, such as a tool's credentials under a listed `~/.config`, the root of another daemon, or an agent state directory that `SPECFLO_AGENT_STATE_DIR` puts inside it. List the narrowest paths a role needs.
- A member cannot work in a checkout below one of its definition's listed directories. The start is refused, because the listed directory holds that checkout's token directories.
- The listed-path checks run when the member's command is built, before bwrap mounts the path. A process that can write a listed path's directory and swaps it for a link in between gets the link's target bound, read-only.
- An agent state directory that `SPECFLO_AGENT_STATE_DIR` puts outside the home is not hidden, so a member can reach the other agents' sockets and read their `events.jsonl`.
- A member can write anything in its working directory, and the operator later runs some of it outside the sandbox, with the network: git hooks, a project `.pi/` extension, a Makefile or an `.envrc`. The same holds for an installation the working directory holds, such as the pool's deny-list extension when the member works in a specflo checkout. The `local` claim covers the member's own process, not what it leaves behind.
- A hosted member shares the host's network, so it reaches every service on the host's loopback: llama-swap on `127.0.0.1:8080` and the daemon's API directly, past the bridge, and any other service listening there. The bridge's held-model check covers local members only.
- The root is bound read-only, so a member of any class reads every disk and mount outside the hidden set, such as a data volume or another user's world-readable files.
- A harness's installation is bound back whole: the directory above the `bin` that holds the program, or the directory it sits in. A harness in a shared prefix such as `~/.local/bin` or `~/.cargo/bin` gives the member everything under `~/.local` or `~/.cargo`, which can hold keyrings and tool credentials. Install the harness under a prefix of its own, as a node version manager does.
- A hosted member shares the host's network namespace, and with it the abstract unix sockets of the operator's session. The X server listens on one (`@/tmp/.X11-unix/X0`), and one that accepts connections from the operator's user lets a hosted member read windows and the clipboard and send input to the desktop outside the sandbox. The session manager's ICE socket is reachable the same way. A local member has its own network namespace and reaches none of them. A hosted member in a network namespace of its own is future work.
- A save from the management pages that passes validation is written even when the reload that follows refuses the swap because a member with a lease out was removed.
- A lease granted to a requester that has gone away, as when the request is cancelled while the member starts, is ended before it is answered and its slots are free at once. Only a lease whose answer was sent and then lost, on a connection that broke as the answer went out, stays out until it expires from idleness.

## The config file

`.specflo/config.yaml` is written by `specflo init` and documents itself. Every
setting specflo has appears in it: live as `key: value` once set, commented out
at its shipped default while it is not, each under a one-line description. So
the file lists what you can change without a trip to the docs, and `config set`
is literally uncommenting a line you can already see.

```yaml
# Where project artifacts live, relative to the repo root.
projects_dir: docs/projects

# The project every command acts on; set it with `specflo switch`.
active_project: my-thing

# Default autonomy level for `specflo auto`: safe, autonomous or yolo.
# autonomy: safe

# Runaway backstop: the most passes one `specflo auto` run may take.
# auto_max_passes: 50

# Percent of the context window at which the pi extension arms clear-and-continue.
# context_threshold_percent: 25

# herdr workspace label where `specflo agent start` places agent tabs.
# agent_space: agents

# Transport a daemon starts a project's agent with: tui (pi in a herdr pane) or rpc.
# agent_transport: tui
```

It is your file, so specflo writes it conservatively:

- A comment you wrote, the key order you chose, and a key specflo has never
  heard of all survive every write.
- Reading never writes. `specflo status --json`, which the pi extension polls
  every turn, leaves the bytes identical.
- A config written before a setting existed gains it on the next write,
  commented out at its default, announced by one note on stderr naming what was
  added.
- A value the file sets but specflo cannot accept degrades to the shipped
  default with a warning, rather than breaking every command that loads it.
  `specflo config list` marks that key `(invalid, using default)`.

Edit the file by hand or go through `specflo config` - the commands validate
before writing, which the editor cannot.

## Session-start integration (clear-and-continue)

An agent can't clear its own context *or* remember what to do across a `/clear` -
the continuation must come from outside the conversation. `specflo hook reseed`
is that bridge: install it once (`specflo hook install`), and on a fresh start,
after a `/clear`, or when you resume a session, Claude Code reorients from the
on-disk checkpoint and **asks before resuming**, so you never re-explain where
you were. Because a `SessionStart` hook can re-ground the agent but cannot make
it speak first, the wiring also surfaces a short visible `systemMessage`
telling you what to type (e.g. `continue`) to start the hand-off.

The installed wiring is Claude Code's. The payload itself is portable: another
harness (opencode, OpenAI Codex, ...) can call the plain-text
`specflo hook reseed` from its own session-start mechanism and inject the
output as context. pi is the exception - it needs no wiring at all, because the
bundled pi extension injects the reseed itself (see
**[The pi extension](#the-pi-extension)**).

**Security posture:** the reseed injects only **trusted local state** - the
checkpoint is derived read-only from the project's own artifacts, never from
external or network input - so running it at session start is benign.

## The pi extension

For the [pi] coding agent, specflo goes further than the hook: a bundled pi
extension performs the clear itself. It is a thin driver by design - every
piece of state it acts on is the stdout of a `specflo` command, it keeps no
durable state of its own, registers no model-callable tool, and never blocks a
tool call. Install it once (pi 0.81+):

```bash
specflo extension install                  # -> ~/.pi/agent/extensions/specflo
specflo extension install --scope project  # -> ./.pi/extensions/specflo
```

pi discovers the directory on its own - nothing else to wire. Re-running is
safe: an up-to-date install is reported as current, a stale one is replaced
whole.

In an attended pi session:

- **Cold start.** When pi starts or resumes inside a specflo repo, the first
  turn is seeded with the `specflo hook reseed` payload, so the agent already
  knows where the project stands and asks before resuming. With no active
  project it injects nothing.
- **Arming.** At each turn's end the extension reads pi's own context-usage
  percent and arms once it reaches `context_threshold_percent` (default `25`,
  set in `.specflo/config.yaml`). Arming is not firing: the next specflo seam
  fires it, so the effective clear point is that percent plus one task's worth
  of context.
- **The seam.** While armed it watches `specflo status --json` for a safe
  point to clear: the phase advancing, or a task reaching done. A task merely
  in progress is never a seam, so in-flight work is never discarded.
- **The notice.** An armed seam produces one passive notice naming the current
  usage, the seam that fired, and the command to run. Nothing clears on its
  own in an attended session.
- **`/specflo-continue`.** Clears the session and reseeds the
  direct-continuation payload - the checkpoint plus the current task brief -
  so the fresh session carries straight on.

In an auto run:

- **`/specflo-continue auto`** starts or continues an unattended run from
  inside pi - the same explicit opt-in `specflo auto` is, counting a pass in
  the same run state - and delivers that pass's payload into a fresh session.
- **The unattended fire.** After one clear has run, every armed seam clears
  and reseeds by itself: the extension fetches the next `specflo auto` pass
  and delivers its payload verbatim. No dialog, no confirmation, no input.
- **Joining a run cold.** If the run was started outside pi (`specflo auto` in
  a terminal, then pi opened), the first armed seam prints a bootstrap notice
  asking you to type `/specflo-continue auto` once; every seam after that is
  unattended.
- **Stopping.** Loop control lives in the CLI, never the extension: on the
  kill switch (`specflo auto --off`), the pass cap, a stall, or project
  completion, nothing clears and the CLI's own stop directive is shown as a
  notice. `specflo auto --on` clears the kill switch again.

[pi]: https://www.npmjs.com/package/@earendil-works/pi-coding-agent

## Skills

specflo ships its eight workflow skills inside the package and installs them into
whatever agent harness it finds on your machine (Claude Code, pi, Hermes,
opencode). Let the CLI do it - no copying or symlinking by hand:

```bash
specflo skills install     # into the user skills dir of every detected harness
specflo skills status      # what is installed, and whether it is current
specflo skills update      # bring stale installs up to the bundled version
specflo skills uninstall   # remove the ones specflo's stamp owns
```

On an interactive terminal, `install` asks which harnesses and scopes to use;
pass `--no-input` (with `--harness`/`--scope`) to script it. Every verb takes
`--scope user|project` - `user` (the default) is the harness's user-level skills
dir such as `~/.claude/skills/`, and `project` is the repo-local one such as
`./.claude/skills/` - plus a repeatable `--harness NAME[:SCOPE]` to target one
harness instead of all detected ones.

Each install is a plain copy carrying a specflo provenance stamp, so `update` and
`uninstall` only ever touch skills specflo itself installed, and a skill you have
edited locally is never overwritten without `--force`. When an installed skill
falls behind the bundled version, any specflo command prints a single advisory
line on stderr pointing at `specflo skills update`. It is notice-only: it never
prompts, never updates anything, and never changes the exit code. Silence it by
setting `CI` or `AGENTSQUIRE_NO_UPDATE_CHECK`.

The eight skills:

- **`specflo-brainstorm`** (`skills/specflo-brainstorm/SKILL.md`) - drives the brainstorm phase over the CLI above (one question at a time, captures decisions, validates, hands off to the spec phase).
- **`specflo-spec`** (`skills/specflo-spec/SKILL.md`) - drives the spec phase (synthesize testable `REQ-NN` requirements from the brainstorm, validate, hand off to the plan phase).
- **`specflo-plan`** (`skills/specflo-plan/SKILL.md`) - drives the plan phase (decompose the validated spec into dependency-ordered, testable `T-NN` tasks, validate, hand off to the execute phase).
- **`specflo-execute`** (`skills/specflo-execute/SKILL.md`) - drives the execute phase (work tasks one at a time with `task show`/`task start`/`task done`, run the final whole-branch review in fresh context and record it with `review start`/`review done`, validate with `validate execute`, complete the project with `advance`).
- **`specflo-quick`** (`skills/specflo-quick/SKILL.md`) - works a quick-level project: fills the brief through the CLI, does the work, records proof, makes one commit, completes the project with no review round, and offers `specflo level fast` when the work outgrows one check.
- **`specflo-research`** (`skills/specflo-research/SKILL.md`) - a research subagent the `specflo-brainstorm` skill dispatches to ground decisions in current facts: an upfront **landscape scan** (what tools/SDKs/clients/frameworks already exist) plus **opportunistic** assumption-checks. Wiki-integrated - searches the Agent Wiki first and saves findings back (soft dependency).
- **`specflo-shelve`** (`skills/specflo-shelve/SKILL.md`) - recognizes "park this for now" / "let's pick that back up" and maps them to `specflo shelve` and `specflo resume`, so a project can be set aside and reclaimed without losing its phase or artifacts.
- **`specflo-auto`** (`skills/specflo-auto/SKILL.md`) - recognizes an unattended-run intent ("auto mode", "autopilot", "keep going without me") and maps it to `specflo auto`, then follows the emitted payload. Thin by design: the CLI carries the loop, autonomy policy, and guardrails; the skill only triggers it and hands the directives to the loop.

### Working on the skills themselves

If you are editing specflo's own skills from a checkout, symlink them instead so
your edits take effect immediately. specflo recognizes a symlinked live-edit
install and stays quiet about updates for it:

```bash
ln -s "$PWD/skills/specflo-brainstorm" ~/.claude/skills/specflo-brainstorm
```

## Install from source

From a checkout, with [uv](https://docs.astral.sh/uv/) installed
(`curl -LsSf https://astral.sh/uv/install.sh | sh`):

```bash
uv tool install .              # install `specflo` globally, from the repo root
uv tool install --reinstall .  # update after pulling changes
uv tool uninstall specflo      # remove
```

`pipx install .` and `pip install .` work the same way if you prefer them.
A machine that runs the daemon needs the web stack too: `uv tool install '.[serve]'`
(or `pip install 'specflo[serve]'`).

To build distributables instead, `uv build` writes the wheel and sdist into
`dist/`; install the wheel anywhere with
`uv tool install ./dist/specflo-<version>-py3-none-any.whl` (no source checkout
needed).

## Development

```bash
uv sync                    # create the venv and install deps (incl. dev group)
uv run pytest              # run the tests
uv run specflo --help      # run the CLI without installing it
```

## License

[GPL-3.0-or-later](https://github.com/TacoTakumi/specflo/blob/main/LICENSE). Copyright (C) 2026 TacoTakumi.
