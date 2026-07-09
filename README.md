# specflo

A spec-driven software-engineering workflow: a Python CLI + skills + subagents,
pluggable into multiple agents. Drives a **brainstorm → spec → plan → execute**
loop over markdown artifacts on disk.

Run `specflo guide` for a one-shot orientation to what specflo is, the pipeline,
and the full command surface.

## v0.1 commands

- `specflo guide [--json]` — orientation in one shot: what specflo is, the pipeline, the full command surface, and what to do next here. Runs **cold** (works before `specflo init`), so a fresh agent can get up to speed in any repo.
- `specflo init` — scaffold `.specflo/config.yaml` + the projects dir (default `docs/projects/`).
- `specflo new <name>` — create a project and make it active.
- `specflo list [--json]` — list all projects, marking the active one and its phase.
- `specflo switch <name>` — make another project active (by slug or name).
- `specflo status [--json]` — show the active project, its phase, and what's next.
- `specflo brainstorm start [--json]` — create (or locate) the active project's `brainstorm.md`.
- `specflo decision add --text … [--rationale …] [--supersedes D-NN]` — append a decision (`D-NN`) to the brainstorm.
- `specflo validate brainstorm [--json]` — lint the brainstorm artifact (reports readiness).
- `specflo spec start [--json]` — create (or locate) the active project's `spec.md`.
- `specflo requirement add --text … --acceptance … [--from D-NN] [--supersedes REQ-NN]` — append a requirement (`REQ-NN`) to the spec.
- `specflo validate spec [--json]` — lint the spec artifact (reports readiness).
- `specflo plan start [--json]` — create (or locate) the active project's `plan.md`.
- `specflo task add --text … --acceptance … --verify … --from REQ-NN [--from REQ-NN …] [--depends-on T-NN …] [--supersedes T-NN]` — append a task (`T-NN`) to the plan. `--from` (repeatable, required) links to the requirement(s) the task implements; `--depends-on` (repeatable) declares execution ordering; `--acceptance` is a behavioural pass/fail criterion; `--verify` is the command or step to confirm it.
- `specflo task start <T-NN>` — mark a task `in_progress`.
- `specflo task done <T-NN>` — mark a task `done`.
- `specflo task block <T-NN> [--reason …]` — mark a task `blocked`, optionally recording why.
- `specflo task reopen <T-NN>` — return a task to `pending` (clears any block).
- `specflo task list [--json]` — list all tasks with their progress state and the deps-aware next-actionable marker.
- `specflo task show [<T-NN>] [--json]` — show a task's brief: acceptance criterion, cited requirements, and constraints. Defaults to the next actionable task.
- `specflo validate plan [--json]` — lint the plan artifact (bidirectional REQ↔task coverage, every task has acceptance + verification, dependencies resolve and are acyclic).
- `specflo validate execute [--json]` — reconcile gate: confirms all tasks are done before the project can be completed.
- `specflo advance [--json]` — validate the current phase's artifact, then move the active project to the next phase (`brainstorm → spec → plan → execute`).
- `specflo checkpoint [--json]` — print the active project's **resume prompt** (which phase, what to read, what to do next) and refresh `checkpoint.md`. The file is also rewritten automatically after every state-mutating command, so a freshly-cleared agent can jump back in with one command.
- `specflo hook reseed [--format text|claude]` — emit the **clear-and-continue** payload for the active project: a confirmation-gate directive (*do not start work; present the checkpoint and ask whether to continue*) followed by the verbatim checkpoint. Default `--format text` is portable plain text (any harness); `--format claude` wraps it as Claude Code `SessionStart` JSON — the payload as `additionalContext` (re-grounds the agent) plus a user-visible `systemMessage` that tells you **what to type** to kick it off (Claude can't make the agent take a turn on its own). Prints nothing for no active project. **Always exits 0, reads no stdin, makes no network calls** — safe to wire into a session-start hook unconditionally.
- `specflo hook print [--install]` — print the `.claude/settings.json` `SessionStart` wiring that calls `specflo hook reseed --format claude` on the `startup`, `clear`, and `resume` sources (`compact` excluded — its digest is retained). `--install` idempotently merges it into `.claude/settings.json`, preserving existing content; a previously-installed (older) reseed entry is rewired in place rather than duplicated.

### Session-start integration (clear-and-continue)

An agent can't clear its own context *or* remember what to do across a `/clear` — the continuation must come from outside the conversation. `specflo hook reseed` is that bridge: install it once (`specflo hook print --install`), and on a fresh start, after a `/clear`, or when you resume a session, Claude Code reorients from the on-disk checkpoint and **asks before resuming**, so you never re-explain where you were. Because a `SessionStart` hook can re-ground the agent but cannot make it speak first, the wiring also surfaces a short visible `systemMessage` telling you what to type (e.g. `continue`) to start the hand-off.

**Security posture:** the reseed injects only **trusted local state** — the checkpoint is derived read-only from the project's own artifacts, never from external or network input — so running it at session start is benign.

## Skills

- **`brainstorm`** (`skills/brainstorm/SKILL.md`) — drives the brainstorm phase over the CLI above (one question at a time, captures decisions, validates, hands off to the spec phase). Install by symlinking it into your agent's skills dir:

  ```bash
  ln -s "$PWD/skills/brainstorm" ~/.claude/skills/brainstorm
  ```

- **`spec`** (`skills/spec/SKILL.md`) — drives the spec phase over the CLI above (synthesize testable `REQ-NN` requirements from the brainstorm, validate, hand off to the plan phase). Install by symlinking it into your agent's skills dir:

  ```bash
  ln -s "$PWD/skills/spec" ~/.claude/skills/spec
  ```

- **`plan`** (`skills/plan/SKILL.md`) — drives the plan phase over the CLI above (decompose the validated spec into dependency-ordered, testable `T-NN` tasks, validate, hand off to the execute phase). Install by symlinking it into your agent's skills dir:

  ```bash
  ln -s "$PWD/skills/plan" ~/.claude/skills/plan
  ```

- **`execute`** (`skills/execute/SKILL.md`) — drives the execute phase over the CLI above (work through tasks one at a time with `task show`/`task start`/`task done`, validate with `validate execute`, complete the project with `advance`). Install by symlinking it into your agent's skills dir:

  ```bash
  ln -s "$PWD/skills/execute" ~/.claude/skills/execute
  ```

- **`research`** (`skills/research/SKILL.md`) — a research subagent the `brainstorm` skill dispatches to ground decisions in current facts: an upfront **landscape scan** (what tools/SDKs/clients/frameworks already exist) plus **opportunistic** assumption-checks. Wiki-integrated — searches the Agent Wiki first and saves findings back (soft dependency). Symlink it like the brainstorm skill:

  ```sh
  ln -s "$PWD/skills/research" ~/.claude/skills/research
  ```

## Requirements

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — used for the environment, dependencies, building, and installing.
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Install the CLI

To produce a `specflo` command on your `PATH`, install the project as a uv tool from a checkout:

```bash
uv tool install .          # from the repo root — installs `specflo` globally
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
