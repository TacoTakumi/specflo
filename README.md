# specflo

**Spec-driven software engineering for coding agents.** specflo is a Python CLI
plus a set of skills and subagents that give an AI agent a disciplined
brainstorm -> spec -> plan -> execute loop, with every artifact (specs, plans,
decisions, tasks) written to plain markdown on disk instead of living in the
model's context window.

**Why on-disk structure matters.** State, validation, and one-task-at-a-time
focus live in the CLI and in files -- not in the model's head. That makes
agentic development more reliable, and it makes real development practical with
capable but smaller *local* models such as Qwen3.6 27B: the agent only has to
reason about the next small, well-scoped, validated step, not hold an entire
project in context.

**Multi-project by design.** specflo tracks many projects in one repo with a
single active project and switch-anytime (`specflo switch`), so a monorepo can
carry several concurrent efforts without them stepping on each other. Each
project keeps its own phase, artifacts, and resume checkpoint.

Pluggable into any harness that can run a CLI and read markdown (Claude Code and
beyond). Run `specflo guide` for a one-shot orientation -- or just **point your
coding agent at this repo and ask it to install and set up specflo**. The
`specflo guide` command is built to orient a fresh agent cold, so it can take it
from there.

## Commands

- `specflo --version` -- print the installed version and exit.
- `specflo guide [--json]` -- orientation in one shot: what specflo is, the pipeline, the full command surface, and what to do next here. Runs **cold** (works before `specflo init`), so a fresh agent can get up to speed in any repo.
- `specflo init` -- scaffold `.specflo/config.yaml` + the projects dir (default `docs/projects/`).
- `specflo new <name>` -- create a project and make it active.
- `specflo list [--json]` -- list all projects, marking the active one and its phase.
- `specflo switch <name>` -- make another project active (by slug or name).
- `specflo status [--json]` -- show the active project, its phase, and what's next.
- `specflo brainstorm start [--json]` -- create (or locate) the active project's `brainstorm.md`.
- `specflo decision add --text ... [--rationale ...] [--supersedes D-NN]` -- append a decision (`D-NN`) to the brainstorm.
- `specflo validate brainstorm [--json]` -- lint the brainstorm artifact (reports readiness).
- `specflo spec start [--json]` -- create (or locate) the active project's `spec.md`.
- `specflo requirement add --text ... --acceptance ... [--from D-NN] [--supersedes REQ-NN]` -- append a requirement (`REQ-NN`) to the spec.
- `specflo validate spec [--json]` -- lint the spec artifact (reports readiness).
- `specflo plan start [--json]` -- create (or locate) the active project's `plan.md`.
- `specflo task add --text ... --acceptance ... --verify ... --from REQ-NN [--from REQ-NN ...] [--depends-on T-NN ...] [--supersedes T-NN]` -- append a task (`T-NN`) to the plan. `--from` (repeatable, required) links to the requirement(s) the task implements; `--depends-on` (repeatable) declares execution ordering; `--acceptance` is a behavioural pass/fail criterion; `--verify` is the command or step to confirm it.
- `specflo task start <T-NN>` -- mark a task `in_progress`.
- `specflo task done <T-NN>` -- mark a task `done`.
- `specflo task block <T-NN> [--reason ...]` -- mark a task `blocked`, optionally recording why.
- `specflo task reopen <T-NN>` -- return a task to `pending` (clears any block).
- `specflo task list [--json]` -- list all tasks with their progress state and the deps-aware next-actionable marker.
- `specflo task show [<T-NN>] [--json]` -- show a task's brief: acceptance criterion, cited requirements, and constraints. Defaults to the next actionable task.
- `specflo validate plan [--json]` -- lint the plan artifact (bidirectional REQ<->task coverage, every task has acceptance + verification, dependencies resolve and are acyclic).
- `specflo validate execute [--json]` -- reconcile gate: confirms all tasks are done before the project can be completed.
- `specflo advance [--json]` -- validate the current phase's artifact, then move the active project to the next phase (`brainstorm -> spec -> plan -> execute`).
- `specflo checkpoint [--json]` -- print the active project's **resume prompt** (which phase, what to read, what to do next) and refresh `checkpoint.md`. The file is also rewritten automatically after every state-mutating command, so a freshly-cleared agent can jump back in with one command.
- `specflo hook reseed [--format text|claude]` -- emit the **clear-and-continue** payload for the active project: a confirmation-gate directive (*do not start work; present the checkpoint and ask whether to continue*) followed by the verbatim checkpoint. Default `--format text` is portable plain text (any harness); `--format claude` wraps it as Claude Code `SessionStart` JSON -- the payload as `additionalContext` (re-grounds the agent) plus a user-visible `systemMessage` that tells you **what to type** to kick it off (Claude can't make the agent take a turn on its own). Prints nothing for no active project. **Always exits 0, reads no stdin, makes no network calls** -- safe to wire into a session-start hook unconditionally.
- `specflo hook print [--install]` -- print the `.claude/settings.json` `SessionStart` wiring that calls `specflo hook reseed --format claude` on the `startup`, `clear`, and `resume` sources (`compact` excluded -- its digest is retained). `--install` idempotently merges it into `.claude/settings.json`, preserving existing content; a previously-installed (older) reseed entry is rewired in place rather than duplicated.
- `specflo auto [--autonomy safe|autonomous|yolo] [--max-passes N]` -- emit the **auto-mode handoff payload** for the active project: the ask-first reseed's opt-in counterpart. An explicit, per-invocation opt-in that starts or continues an *unattended* run from the current phase toward project completion, emitting a bootstrap directive (autonomy policy + guardrail stop-conditions) instead of the confirmation-gate pause. specflo only prints the payload -- it drives no loop, spawns no nested agent, and never clears context; the seamless clear-and-reseed trigger is the outer harness's job. `--autonomy` sets how far it runs unattended: `safe` (the default) and `autonomous` stop and hand off on any irreversible or outbound step, `yolo` permits them; the flag overrides the `.specflo` config default. `--max-passes` is a runaway backstop: each invocation counts as one pass in a durable per-project run-state file, and on reaching the cap (default `50`) the run escalates to the human instead of continuing; the flag overrides the config default. `--json` reports the same pass as an object -- its `payload` text, a boolean `stop`, and the `reason` that stopped it (`kill-switch`, `pass-cap`, `stall`, `project-complete`, or `unavailable`; `null` while the run continues) -- so a machine caller reads loop control from the CLI instead of deciding it. Strictly additive -- the default `hook reseed` / checkpoint behavior is unchanged.
- `specflo extension install [--scope user|project]` - install the bundled pi extension into pi's extension directory: `~/.pi/agent/extensions/specflo` by default, `./.pi/extensions/specflo` with `--scope project`. A plain local copy with a version stamp - no npm, no network - and pi discovers the directory on its own, so no pi settings are read or written. Re-run to update. See **[The pi extension](#the-pi-extension)** below.
- `specflo skills install|status|update|uninstall [--scope user|project] [--harness NAME[:SCOPE]]` -- install specflo's bundled workflow skills into the agent harnesses on your machine, and keep them current. See **[Skills](#skills)** below.

### Session-start integration (clear-and-continue)

An agent can't clear its own context *or* remember what to do across a `/clear` -- the continuation must come from outside the conversation. `specflo hook reseed` is that bridge: install it once (`specflo hook print --install`), and on a fresh start, after a `/clear`, or when you resume a session, Claude Code reorients from the on-disk checkpoint and **asks before resuming**, so you never re-explain where you were. Because a `SessionStart` hook can re-ground the agent but cannot make it speak first, the wiring also surfaces a short visible `systemMessage` telling you what to type (e.g. `continue`) to start the hand-off.

**Security posture:** the reseed injects only **trusted local state** -- the checkpoint is derived read-only from the project's own artifacts, never from external or network input -- so running it at session start is benign.

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
  percent and arms once it reaches `context_threshold_percent` (default `75`,
  set in `.specflo/config.yaml`).
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
opencode). Let the CLI do it -- no copying or symlinking by hand:

```bash
specflo skills install     # into the user skills dir of every detected harness
specflo skills status      # what is installed, and whether it is current
specflo skills update      # bring stale installs up to the bundled version
specflo skills uninstall   # remove the ones specflo's stamp owns
```

On an interactive terminal, `install` asks which harnesses and scopes to use;
pass `--no-input` (with `--harness`/`--scope`) to script it. Every verb takes
`--scope user|project` -- `user` (the default) is the harness's user-level skills
dir such as `~/.claude/skills/`, and `project` is the repo-local one such as
`./.claude/skills/` -- plus a repeatable `--harness NAME[:SCOPE]` to target one
harness instead of all detected ones.

Each install is a plain copy carrying a specflo provenance stamp, so `update` and
`uninstall` only ever touch skills specflo itself installed, and a skill you have
edited locally is never overwritten without `--force`. When an installed skill
falls behind the bundled version, any specflo command prints a single advisory
line on stderr pointing at `specflo skills update`. It is notice-only: it never
prompts, never updates anything, and never changes the exit code. Silence it by
setting `CI` or `AGENTSQUIRE_NO_UPDATE_CHECK`.

The seven skills:

- **`brainstorm`** (`skills/brainstorm/SKILL.md`) -- drives the brainstorm phase over the CLI above (one question at a time, captures decisions, validates, hands off to the spec phase).
- **`spec`** (`skills/spec/SKILL.md`) -- drives the spec phase (synthesize testable `REQ-NN` requirements from the brainstorm, validate, hand off to the plan phase).
- **`plan`** (`skills/plan/SKILL.md`) -- drives the plan phase (decompose the validated spec into dependency-ordered, testable `T-NN` tasks, validate, hand off to the execute phase).
- **`execute`** (`skills/execute/SKILL.md`) -- drives the execute phase (work tasks one at a time with `task show`/`task start`/`task done`, validate with `validate execute`, complete the project with `advance`).
- **`research`** (`skills/research/SKILL.md`) -- a research subagent the `brainstorm` skill dispatches to ground decisions in current facts: an upfront **landscape scan** (what tools/SDKs/clients/frameworks already exist) plus **opportunistic** assumption-checks. Wiki-integrated -- searches the Agent Wiki first and saves findings back (soft dependency).
- **`shelve`** (`skills/shelve/SKILL.md`) -- recognizes "park this for now" / "let's pick that back up" and maps them to `specflo shelve` and `specflo resume`, so a project can be set aside and reclaimed without losing its phase or artifacts.
- **`auto`** (`skills/auto/SKILL.md`) -- recognizes an unattended-run intent ("auto mode", "autopilot", "keep going without me") and maps it to `specflo auto`, then follows the emitted payload. Thin by design: the CLI carries the loop, autonomy policy, and guardrails; the skill only triggers it and hands the directives to the loop.

### Working on the skills themselves

If you are editing specflo's own skills from a checkout, symlink them instead so
your edits take effect immediately. specflo recognizes a symlinked live-edit
install and stays quiet about updates for it:

```bash
ln -s "$PWD/skills/brainstorm" ~/.claude/skills/brainstorm
```

## Requirements

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** -- used for the environment, dependencies, building, and installing.
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Install the CLI

The easiest path: **point your coding agent at this repo and ask it to install
specflo** -- then run `specflo guide` and let it orient itself.

To do it by hand, produce a `specflo` command on your `PATH` by installing the
project as a uv tool from a checkout:

```bash
uv tool install .          # from the repo root -- installs `specflo` globally
specflo --help             # verify it's on PATH
```

Reinstall after pulling changes with `uv tool install --reinstall .` (or `uv tool upgrade specflo`).
Uninstall with `uv tool uninstall specflo`.

Equivalent alternatives if you prefer other installers:

```bash
pipx install .             # via pipx
pip install .              # into the active environment/venv
```

## Build distributables

Build a wheel and source distribution into `dist/`:

```bash
uv build                   # writes dist/specflo-<version>-py3-none-any.whl and .tar.gz
```

Install the built wheel anywhere (no source checkout needed):

```bash
uv tool install ./dist/specflo-0.1.0-py3-none-any.whl
# or: pipx install ./dist/specflo-0.1.0-py3-none-any.whl
```

## Development

```bash
uv sync                    # create the venv and install deps (incl. dev group)
uv run pytest              # run the tests
uv run specflo --help      # run the CLI without installing it
```
