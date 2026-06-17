# specflo

A spec-driven software-engineering workflow: a Python CLI + skills + subagents,
pluggable into multiple agents. Drives a **brainstorm → spec → plan → execute**
loop over markdown artifacts on disk.

See `docs/MASTER.md` for project status and `docs/intent.md` for the vision.

## v0.1 commands

- `specflo init` — scaffold `.specflo/config.yaml` + the projects dir (default `docs/projects/`).
- `specflo new <name>` — create a project and make it active.
- `specflo list [--json]` — list all projects, marking the active one and its phase.
- `specflo switch <name>` — make another project active (by slug or name).
- `specflo status [--json]` — show the active project, its phase, and what's next.
- `specflo brainstorm start [--json]` — create (or locate) the active project's `brainstorm.md`.
- `specflo decision add --text … [--rationale …] [--supersedes D-NN]` — append a decision (`D-NN`) to the brainstorm.
- `specflo validate brainstorm [--json]` — lint the brainstorm artifact (reports readiness).
- `specflo advance [--json]` — validate the current phase's artifact, then move the active project to the next phase (`brainstorm → spec → plan → execute`).

## Skills

- **`brainstorm`** (`skills/brainstorm/SKILL.md`) — drives the brainstorm phase over the CLI above (one question at a time, captures decisions, validates, hands off to the spec phase). Install by symlinking it into your agent's skills dir:

  ```bash
  ln -s "$PWD/skills/brainstorm" ~/.claude/skills/brainstorm
  ```

- **`research`** (`skills/research/SKILL.md`) — a research subagent the `brainstorm` skill dispatches to ground decisions in current facts: an upfront **landscape scan** (what tools/SDKs/clients/frameworks already exist) plus **opportunistic** assumption-checks. Wiki-integrated — searches the Agent Wiki first and saves findings back (soft dependency). Symlink it like the brainstorm skill:

  ```sh
  ln -s "$PWD/skills/research" ~/.claude/skills/research
  ```

## Development

```bash
uv sync        # create the venv and install deps
uv run pytest  # run the tests
uv run specflo --help
```
