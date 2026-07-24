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
4. **execute** - work tasks one at a time; a reconcile gate confirms every task is done before the project completes.

`specflo advance` validates the current phase's artifact before moving on, so a
hole in the spec stops the line early instead of surfacing mid-execution.
Artifacts are plain markdown under `docs/projects/<slug>/` (configurable):

```text
$ specflo status
Project: Payment retries (payment-retries)
Dir:     docs/projects/payment-retries
Phase:   brainstorm
Next:    Brainstorm and research; capture decisions, then write the spec.
Resume:  specflo checkpoint
```

Clearing context is free at any point: `checkpoint.md` is rewritten after every
state change, and `specflo checkpoint` prints the resume prompt that puts a
fresh session back to work.

## Command reference

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

- `specflo new <name>` - create a project and make it active.
- `specflo list [--json]` - list all projects, marking the active one and its phase.
- `specflo switch <name>` - make another project active (by slug or name).
- `specflo status [--json]` - show the active project, its phase, and what's next.
- `specflo shelve [<name>] [--reason ...]` - set a project aside: status `shelved`, phase untouched.
- `specflo resume [<name>]` - pick a shelved project back up at the phase where it was paused.

### Phase artifacts

- `specflo brainstorm start [--json]` - create (or locate) the active project's `brainstorm.md`.
- `specflo decision add --text ... [--rationale ...] [--supersedes D-NN]` - append a decision (`D-NN`) to the brainstorm.
- `specflo spec start [--json]` - create (or locate) the active project's `spec.md`.
- `specflo requirement add --text ... --acceptance ... [--from D-NN] [--supersedes REQ-NN]` - append a requirement (`REQ-NN`) to the spec.
- `specflo plan start [--json]` - create (or locate) the active project's `plan.md`.
- `specflo task add --text ... --acceptance ... --verify ... --from REQ-NN [--from REQ-NN ...] [--depends-on T-NN ...] [--supersedes T-NN]` - append a task (`T-NN`) to the plan. `--from` (repeatable, required) links to the requirement(s) the task implements; `--depends-on` (repeatable) declares execution ordering; `--acceptance` is a behavioural pass/fail criterion; `--verify` is the command or step to confirm it.
- `specflo milestone add --text ... --exit ... [--exit ...]` - append a milestone (`M-NN`) with its Exit checklist to the plan; `milestone list` and `milestone show` report rollup and the current milestone.
- `specflo validate brainstorm|spec|plan [--json]` - lint the phase's artifact and report readiness. The plan lint checks bidirectional REQ<->task coverage, that every task has acceptance + verification, and that dependencies resolve and are acyclic.

### Working the plan

- `specflo task start <T-NN>` / `task done <T-NN>` - mark a task `in_progress` / `done`.
- `specflo task block <T-NN> [--reason ...]` / `task reopen <T-NN>` - mark a task `blocked` (optionally recording why) / return it to `pending`.
- `specflo task list [--json]` - all tasks with their progress state and the deps-aware next-actionable marker.
- `specflo task show [<T-NN>] [--json]` - a task's brief: acceptance criterion, cited requirements, and constraints. Defaults to the next actionable task.
- `specflo validate execute [--json]` - reconcile gate: confirms all tasks are done before the project can be completed.
- `specflo advance [--json]` - validate the current phase's artifact, then move the active project to the next phase (`brainstorm -> spec -> plan -> execute`).
- `specflo reopen [<phase>]` - the inverse of `advance`: move the phase pointer backward (bare `reopen` goes one phase back, `reopen <phase>` jumps to a named earlier phase). A pure pointer move; no artifact is rewritten.
- `specflo checkpoint [--json]` - print the active project's **resume prompt** (which phase, what to read, what to do next) and refresh `checkpoint.md`. The file is also rewritten automatically after every state-mutating command, so a freshly-cleared agent can jump back in with one command.

### Session-start and unattended runs

- `specflo hook reseed [--format text|claude] [--continue]` - emit the **clear-and-continue** payload for the active project: a confirmation-gate directive (*do not start work; present the checkpoint and ask whether to continue*) followed by the verbatim checkpoint. Prints nothing for no active project. **Always exits 0, reads no stdin, makes no network calls** - safe to wire into a session-start hook unconditionally.
  - `--format claude` wraps the payload as Claude Code `SessionStart` JSON: the payload as `additionalContext` (re-grounds the agent) plus a user-visible `systemMessage` that tells you **what to type** to kick it off (Claude can't make the agent take a turn on its own). The default `--format text` stays portable plain text for any harness.
  - `--continue` swaps the confirmation gate for a direct *carry out the next step now* directive, and inlines the current task's brief - for a caller that cleared context on purpose and has already answered "keep going".
- `specflo hook install` - idempotently merge the `SessionStart` wiring into Claude Code's `.claude/settings.json`, preserving all existing content; a previously-installed (older) reseed entry is rewired in place rather than duplicated. The wiring calls `specflo hook reseed --format claude` on the `startup`, `clear`, and `resume` sources (`compact` excluded - its digest is retained).
- `specflo hook print` - print that same wiring as a JSON fragment on stdout (pipeable), either to merge into Claude Code's settings yourself or as a starting point to adapt for another harness (opencode, OpenAI Codex, ...); a stderr note marks it as a fragment and points at `specflo hook install` as the safe merge. pi needs no wiring - the bundled pi extension reseeds on its own (see **[The pi extension](#the-pi-extension)**). (`hook print --install` remains as a deprecated alias of `hook install`.)
- `specflo auto [--autonomy safe|autonomous|yolo] [--max-passes N] [--off|--on] [--json]` - emit the **auto-mode handoff payload**: an explicit, per-invocation opt-in that starts or continues an *unattended* run from the current phase toward project completion, emitting a bootstrap directive (autonomy policy + guardrail stop-conditions) instead of the ask-first pause. specflo only prints the payload - it drives no loop, spawns no nested agent, and never clears context; the clear-and-reseed trigger is the outer harness's job. Strictly additive - the default `hook reseed` / checkpoint behavior is unchanged.
  - `--autonomy` sets how far it runs unattended: `safe` (the default) and `autonomous` stop and hand off on any irreversible or outbound step; `yolo` permits them. Overrides the `.specflo` config default.
  - `--max-passes` is a runaway backstop: each invocation counts as one pass in a durable per-project run-state file, and on reaching the cap (default `50`) the run escalates to the human instead of continuing. Overrides the config default.
  - `--off` sets the durable kill switch (the next pass halts); `--on` clears it.
  - `--json` reports the pass as an object - its `payload` text, a boolean `stop`, and the `reason` that stopped it (`kill-switch`, `pass-cap`, `stall`, `project-complete`, or `unavailable`; `null` while the run continues) - so a machine caller reads loop control from the CLI instead of deciding it.

### Harness integration

- `specflo skills install|status|update|uninstall [--scope user|project] [--harness NAME[:SCOPE]]` - install specflo's bundled workflow skills into the agent harnesses on your machine, and keep them current. See **[Skills](#skills)**.
- `specflo extension install [--scope user|project]` - install the bundled pi extension into pi's extension directory: `~/.pi/agent/extensions/specflo` by default, `./.pi/extensions/specflo` with `--scope project`. A plain local copy with a version stamp - no npm, no network - and pi discovers the directory on its own, so no pi settings are read or written. Re-run to update. See **[The pi extension](#the-pi-extension)**.

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

specflo ships its seven workflow skills inside the package and installs them into
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

The seven skills:

- **`specflo-brainstorm`** (`skills/specflo-brainstorm/SKILL.md`) - drives the brainstorm phase over the CLI above (one question at a time, captures decisions, validates, hands off to the spec phase).
- **`specflo-spec`** (`skills/specflo-spec/SKILL.md`) - drives the spec phase (synthesize testable `REQ-NN` requirements from the brainstorm, validate, hand off to the plan phase).
- **`specflo-plan`** (`skills/specflo-plan/SKILL.md`) - drives the plan phase (decompose the validated spec into dependency-ordered, testable `T-NN` tasks, validate, hand off to the execute phase).
- **`specflo-execute`** (`skills/specflo-execute/SKILL.md`) - drives the execute phase (work tasks one at a time with `task show`/`task start`/`task done`, validate with `validate execute`, complete the project with `advance`).
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
