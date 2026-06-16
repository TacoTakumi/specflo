# specflo

A spec-driven software-engineering workflow: a Python CLI + skills + subagents,
pluggable into multiple agents. Drives a **brainstorm → spec → plan → execute**
loop over markdown artifacts on disk.

See `docs/MASTER.md` for project status and `docs/intent.md` for the vision.

## v0.1 commands

- `specflo init` — scaffold `.specflo/config.yaml` + the projects dir (default `docs/projects/`).
- `specflo new <name>` — create a project and make it active.
- `specflo status [--json]` — show the active project, its phase, and what's next.

## Development

```bash
uv sync        # create the venv and install deps
uv run pytest  # run the tests
uv run specflo --help
```
