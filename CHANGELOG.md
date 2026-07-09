# Changelog

All notable changes to specflo are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is kept in sync across `version` in `pyproject.toml` and
`__version__` in `src/specflo/__init__.py`; `specflo --version` derives from the
latter. Release tags are of the form `vX.Y.Z`.

## [0.1.1]

### Added
- **Paste-ready memory snippet in `specflo guide`.** The guide output (and its
  `--json` payload) now leads with a thin, version-less block you paste once into
  an agent memory file (CLAUDE.md / AGENTS.md) so a cold agent discovers specflo
  without being told. It points at `specflo guide` / `specflo status` rather than
  embedding the command surface, so it never goes stale and never needs
  re-committing on upgrade.

### Changed
- **README rewrite.** Stronger intro pitch (why on-disk structure matters, the
  local-model angle, multi-project/switching) plus a "point your agent at this
  repo and ask it to install" path. README and CHANGELOG are now pure ASCII,
  enforced by `tests/test_docs_ascii.py`.

## [0.1.0]

Initial public release.

### Added
- **Spec-driven pipeline CLI.** `specflo` drives a linear
  **brainstorm -> spec -> plan -> execute** loop over markdown artifacts on disk,
  with active-project tracking and switch-anytime support (monorepo-friendly).
- **Orientation & scaffolding.** `specflo guide` (runs cold, before `init`),
  `specflo init`, `new`, `list`, `switch`, `status`.
- **Artifact commands.** `brainstorm start` + `decision add`; `spec start` +
  `requirement add`; `plan start` + `task add`/`start`/`done`/`block`/`reopen`/
  `list`/`show`; per-phase `validate` (brainstorm, spec, plan, execute) with
  bidirectional REQ<->task coverage and acyclic dependency checks.
- **Phase transitions.** `advance` (validate-then-move) and `reopen` (reversible
  phases), plus `checkpoint` for a resume prompt refreshed after every
  state-mutating command.
- **Session-start integration.** `specflo hook reseed` / `hook print [--install]`
  wire a clear-and-continue bridge into Claude Code's `SessionStart` hook,
  re-grounding an agent from on-disk state after `/clear` or resume.
- **Skills.** `brainstorm`, `spec`, `plan`, `execute`, and a `research` subagent
  that drive each phase over the CLI.
