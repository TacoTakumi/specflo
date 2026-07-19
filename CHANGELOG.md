# Changelog

All notable changes to specflo are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is kept in sync across `version` in `pyproject.toml` and
`__version__` in `src/specflo/__init__.py`; `specflo --version` derives from the
latter. Release tags are of the form `vX.Y.Z`.

## [0.3.0]

### Added
- **`specflo auto` command (auto mode).** An explicit, per-invocation opt-in that
  emits an auto-mode *handoff payload* -- a bootstrap directive (autonomy policy
  plus guardrail stop-conditions) that lets an agent run the specflo pipeline
  unattended from the current phase toward completion, instead of the ask-first
  confirmation pause. specflo only prints the payload; it drives no loop, spawns
  no nested agent, and never clears context (the outer harness owns the trigger).
  Strictly additive: the default `hook reseed` / checkpoint pause is unchanged.
- **`--autonomy {safe,autonomous,yolo}`.** Governs how far an auto run goes
  unattended, with a matching `.specflo` config default (default `safe`). `safe`
  and `autonomous` stop and hand off on any irreversible or outbound step; `yolo`
  permits them. The flag overrides the config default.
- **`--max-passes N` iteration cap.** A runaway backstop on the auto loop: each
  `specflo auto` invocation counts as one pass in a durable per-project run-state
  file, and on reaching the cap (default `50`) the run escalates to the human
  instead of continuing. Backed by a matching `.specflo` config default; the flag
  overrides it. The run-state is ephemeral -- never a persisted auto-on default.
- **Self-contained three-part `specflo auto` payload.** The continue payload is
  now everything a freshly-cleared session needs in one block: the auto-mode
  bootstrap, the verbatim `specflo checkpoint` text (read-first files, next
  action, milestone beat), and a compact generated next-step block naming the
  current phase, its immediate next action, and the phase skill to run. A resumed
  session can act on the payload alone without re-deriving state.
- **Self-propagating bootstrap.** The auto-mode bootstrap now instructs each
  continuing pass to re-emit it (re-run `specflo auto`, never the manual ask-first
  reseed), so the loop keeps its autonomy policy across a context clear and does
  not silently revert to ask-first after the first phase boundary.

## [0.2.0]

### Added
- **`specflo skills` command group.** specflo now carries its 6 workflow skills
  (brainstorm, execute, plan, research, shelve, spec) as bundled package data and
  installs them into whatever agent harness is present (Claude Code, pi, Hermes,
  opencode): `specflo skills install` / `status` / `update` / `uninstall`. Each
  install is a plain copy carrying a specflo provenance stamp, honors
  `--scope user|project` (default user) and `--harness`, and never clobbers a
  locally-modified skill without `--force`. Powered by agentsquire, now a runtime
  dependency (`agentsquire>=0.5.0`).
- **Startup stale-skills notice.** When installed skills have an update
  available, any specflo command prints a single advisory line on stderr pointing
  at `specflo skills update`. It is notice-only: it never prompts, never updates
  anything, never writes stdout, and never changes the exit code, and it stays
  silent for a symlinked live-edit dev install. Suppress it with `CI` or
  `AGENTSQUIRE_NO_UPDATE_CHECK` set to any non-empty value.
- **`specflo --version`.** A top-level `--version` option prints the released
  version (`specflo <X.Y.Z>`) and exits 0.

### Changed
- **`click` is now a declared dependency** (`click>=8.1`). The CLI imports it
  directly to catch click's control-flow exceptions; previously it relied on
  typer and agentsquire to pull it in transitively. No behaviour change - the
  same click was already being installed.

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
