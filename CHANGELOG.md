# Changelog

All notable changes to specflo are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is kept in sync across `version` in `pyproject.toml` and
`__version__` in `src/specflo/__init__.py`; `specflo --version` derives from the
latter. Release tags are of the form `vX.Y.Z`.

## [0.4.0]

### Added
- **`specflo hook reseed --continue`.** Emits a *direct-continuation* payload:
  an imperative "carry out the next step now" instruction in place of the
  ask-first confirmation directive, followed by the same verbatim checkpoint.
  The ask-first gate exists because a cold-start session hook cannot know
  whether the human wants to keep going; a caller that cleared context on
  purpose has already answered that, so re-asking wastes the turn the clear
  just bought. Bare `specflo hook reseed` is unchanged, byte for byte. The flag
  is rejected with `--format claude`, whose user-visible nudge asks the user to
  type `continue` and would contradict the payload. A complete or shelved
  project still gets its own directive -- neither has a next step to carry out.
- **The direct-continuation payload inlines the current task brief.** In the
  execute phase `hook reseed --continue` appends a `## Current task brief`
  section carrying the task's acceptance, its verify step, the full text of
  every REQ-NN it cites, and the plan's global constraints -- the same render
  `specflo task show` prints, so a reseeded session need not spend its first
  turn retrieving it. The brief is the task actually being worked (the first
  `in_progress` one, not the next pending one). Bounded: one brief, no decision
  bodies, and nothing at all in brainstorm, spec or plan. Enrichment only -- an
  unreadable plan still yields the directive and the checkpoint.

- **`context_threshold_percent` config key.** The percent of the model context
  window at which the pi extension arms its clear-and-continue trigger, set in
  `.specflo/config.yaml` alongside `autonomy` and `auto_max_passes`. Defaults to
  `75` and, like those two, is written out only when it differs from the
  default, so a plain project's config carries no tuning key. A percent rather
  than a token count, so it holds across models with different window sizes. An
  unusable value (non-integer, boolean, or outside 1-100) degrades to `75`
  rather than raising, so a hand-edited config cannot break every command.
- **`specflo status --json` reports `context_threshold_percent`.** The resolved
  arming percent rides on the status payload the pi extension already fetches at
  cold start, so the extension never opens `.specflo/config.yaml` itself. Always
  present, always an integer: `75` with no key set, the configured value once
  set. The human `specflo status` block is unchanged.
- **`specflo auto --json`.** Emits one pass as an object: the `payload` text the
  prose path prints byte for byte, a boolean `stop`, and the `reason` that
  stopped it -- `kill-switch`, `pass-cap`, `stall`, `project-complete`, or
  `unavailable` (no specflo root, no active project, or an unreadable one), and
  `null` while the run continues. Loop control is therefore read from the CLI
  rather than re-derived: a machine caller needs no kill-switch check, pass
  counter or cap of its own. Rejected with `--off`/`--on`, which run no pass.
  The default `specflo auto` output is unchanged.

### Changed
- **All four reseed directives now live in `continuation.py`**, which becomes
  the single producer of payload prose; `hook.py` selects between them and
  re-exports the names. No output changes.

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
- **`auto` workflow skill.** A thin skill (the seventh) that recognizes an
  unattended-run intent and maps it to `specflo auto`, then follows the emitted
  payload. It reimplements none of the loop/guardrail/payload logic -- the CLI
  carries that -- mirroring specflo's skill-vs-CLI split. Installed by
  `specflo skills install` alongside the others.
- **Clear-point and continue-instruction at `specflo task done`.** Completing a
  task previously printed only `T-NN -> done`, so the only thing telling an agent
  it could clear context between tasks was prose in the execute skill. It now
  emits the same four-part shape `specflo advance` does: the transition line, the
  derived next-step hint naming the next actionable task, the saved checkpoint
  location, and a clear-point naming both resume paths -- `specflo checkpoint`,
  and `specflo auto` for an unattended run. The hint comes from the same
  derivation `status` and `checkpoint` use, so it cannot drift from them.
- **Continuation fields in both seams' `--json`.** `specflo task done --json`
  gains `next_step` and `checkpoint` (matching what `advance` already emitted),
  and both seams gain a `continuation` field carrying the rendered text, so a
  harness can consume the clear-point without parsing prose. At project
  completion the field carries the clear-point-only form. The keys are always
  present: if the continuation cannot be derived they carry `null` and an
  advisory goes to stderr, rather than the keys silently going missing.

### Changed
- **Phase advance and reopen emit the shared continuation.** The clear-point line
  at every seam is now produced by one shared builder, so the wording cannot
  drift between them. Advancing into a phase names the specflo phase skill that
  carries it; `specflo reopen` gains that same pointer and the auto resume path.
  Completing a project deliberately emits a clear-point with *no*
  continue-instruction and names neither resume command, so an auto loop halting
  on the completion signal is never invited to start another pass. The emitted
  text stays harness-neutral (specflo commands only) and identical whether or not
  an auto run is under way -- specflo owns the payload, never the trigger.

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
