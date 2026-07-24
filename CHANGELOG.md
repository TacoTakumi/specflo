# Changelog

All notable changes to specflo are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is kept in sync across `version` in `pyproject.toml` and
`__version__` in `src/specflo/__init__.py`; `specflo --version` derives from the
latter. Release tags are of the form `vX.Y.Z`.

## [0.5.0]

### Added
- **`specflo config` - `get`, `set`, `list`, `unset`.** Every setting is now
  readable and changeable from the CLI instead of by opening the file. `config
  get <key>` prints the resolved value bare on stdout, so `$(specflo config get
  autonomy)` is the value itself. `config list [--json]` shows every setting
  with its value, marking `(default)` where the file is silent and `(invalid,
  using default)` where its value is not one the key accepts; keys specflo does
  not recognize are listed separately and left alone. `config set` coerces and
  validates before writing, so a rejected value leaves the file untouched and
  the error names what the key accepts. `config unset` returns a key to its
  commented-out default. `active_project` stays readable but is not writable
  here (`specflo switch` owns it), and `projects_dir` needs `--force` while
  projects live under the current path: changing it moves nothing, it only
  changes where specflo looks.
- **The config file documents itself.** A fresh `specflo init` writes a
  `.specflo/config.yaml` carrying every setting specflo has - live as
  `key: value` once set, commented out at its shipped default while it is not,
  each under a one-line description. The file lists what you can change without
  a trip to the docs, and `config set` reads as uncommenting a line you can
  already see. A config written before a setting existed gains it on the next
  write, announced by one stderr note naming what was added.

### Changed
- **BREAKING: every bundled skill is renamed with a `specflo-` prefix.** The
  seven skills are now `specflo-brainstorm`, `specflo-spec`, `specflo-plan`,
  `specflo-execute`, `specflo-research`, `specflo-shelve`, and `specflo-auto`
  (previously the bare phase names), so generic names like `plan` or `auto` no
  longer collide with other installed skills or a harness's built-ins. The
  phase names inside specflo are unchanged; only the skill/command names moved.
  After updating, run `specflo skills update` and remove any old-name installs
  (symlinked dev setups: re-link, e.g. `~/.claude/skills/specflo-plan ->
  skills/specflo-plan`). Checkpoint and auto payloads now point at the new
  names.
- **`context_threshold_percent` now defaults to 25, not 75.** The pi
  extension's arming threshold is a percent of the context window, and arming
  is not firing: the next specflo seam fires it, so the effective clear point
  is the threshold plus one task's worth of context. pi auto-compacts near 92
  percent and compaction disarms the extension, so arming at 75 could miss the
  window entirely; arming early costs one bounded reseed. Existing configs that
  set the key are unaffected.
- **A config write preserves everything specflo does not own.** Saves now
  round-trip the file rather than regenerating it, so a comment you wrote, the
  key order you chose, and a key specflo has never heard of all survive
  `specflo new`, `switch`, `resume`, and the `config` commands. Reading still
  never writes: `specflo status --json`, which the pi extension polls every
  turn, leaves the bytes identical.
- **An unusable value in the config no longer breaks the commands that load
  it.** A value the file sets but the setting cannot accept degrades to the
  shipped default with one stderr warning per key, instead of raising - a
  hand-edited config must not take down `status --json` and with it the
  clear-and-continue trigger. Set-time validation still rejects outright, so
  the bad value has to arrive by hand-editing.
- **The `specflo guide` memory snippet is now the README's onboarding blurb.**
  `guide` prints the README "Development workflow" section verbatim, heading
  included, so it drops into `CLAUDE.md` / `AGENTS.md` as a section and carries
  the norm the old wording left out: record decisions, requirements, and tasks
  through the CLI rather than editing the artifacts by hand. The surrounding
  instruction now says to paste it near the top of the memory file, where a
  fresh agent is most likely to act on it. README.md is the authority for that
  text and a test asserts the CLI copy stays byte-identical to it, so editing
  one alone fails the suite instead of shipping two different blurbs.
- **README Quick start lists `specflo extension install`.** pi users had to
  reach the pi extension section to learn their setup step; the setup block now
  shows it next to `hook install`, with both marked as per-harness alternatives
  so nobody runs the wrong one or assumes both are required.

### Fixed
- **The unattended auto chain now fires at every seam, not just the first.**
  The pi extension's reseed waited for the whole agent run its payload
  started before releasing the in-flight latch, and that run is a full auto
  pass - minutes. Every seam declared during it was latched out, so an auto
  run cleared once and then sailed on with the context filling up and nothing
  to show why. The reseed is now dispatched rather than awaited: the latch
  releases as soon as the replacement session has the payload, so the next
  seam fires normally. A reseed that fails to send drops the anchor, and the
  next armed seam falls back to the notice naming `/specflo-continue auto`.
  Nothing else moves - arming, the threshold, seam detection and the attended
  and unanchored branches are unchanged.
- **An unattended auto seam now ends the running agent.** The pi extension's
  armed, anchored seam during an auto run used to park the clear behind
  waitForIdle and let the run keep going, so the agent sailed past the
  threshold from task to task and the clear only landed at the run's natural
  end. The seam now calls ctx.abort() after parking the fire: the run stops
  at the seam, the parked continuation lands, and the fresh session opens on
  the auto payload. The attended and unanchored branches are unchanged - they
  still only notify.
- **A duplicated key in the config refuses the write cleanly.** A
  hand-duplicated line used to crash every writing command (`new`, `switch`,
  `resume`, `config set`, `config unset`) with a raw traceback; it is now a
  one-line error naming the file and key, exit 1, file untouched. Reads keep
  tolerating the file (the last copy of the key wins) so `status --json`
  never stalls on it.
- **A write no longer swallows another key's invalid value.** Setting one key
  used to silently rewrite an unrelated key's unusable value to the shipped
  default, destroying what the user had typed. The bad value now stays in the
  file exactly as written - degraded in memory and marked
  `(invalid, using default)` by `config list` - until the user fixes it, sets
  the key, or unsets it.
- **A config file that is not a mapping reads as empty.** A bare string in
  the file made `config list` enumerate the string's characters as unknown
  keys, and a string containing a key name crashed every read. Reads now
  treat a non-mapping document as holding no keys, every setting at its
  default - the same reading the write side already used.
- **`config unset` keeps a comment written beside the key.** The end-of-line
  comment on `autonomy: yolo  # tuned for CI` used to vanish with the value;
  it now lands on a line of its own where the key was, as a comment under the
  key always did.

## [0.4.1]

### Added
- **`specflo hook install`.** The SessionStart-wiring installer is now a
  first-class subcommand, visible in `specflo hook --help`. It idempotently
  merges the reseed entry into Claude Code's `.claude/settings.json`,
  preserving all existing content (same merge as before -- only the spelling
  is new). Previously the installer hid behind `hook print --install`, where a
  mutating action sat on a printing command and the flag was invisible one
  level up.

- **PyPI project metadata.** `pyproject.toml` now declares the license
  (`GPL-3.0-or-later` SPDX expression + bundled LICENSE file, whose header
  now carries the standard or-later grant notice), the project URLs
  (homepage, repository, changelog, issues), keywords, and trove classifiers,
  so the PyPI page links back to the repo and states the license instead of
  rendering bare.

### Changed
- **README restructured for onboarding.** The front door now reads in
  newcomer order: a PyPI-first Quick start (install, `init`, `skills install`,
  `hook install`, and the first thing to say to your agent), a "Using specflo
  with your agent" section with a copy-paste CLAUDE.md/AGENTS.md blurb and the
  prompts that map onto the workflow, a pipeline overview with real
  `specflo status` output, and a command reference grouped by area. Install
  from source is one blessed command per situation. Previously undocumented
  commands are now in the reference: `shelve`, `resume`, `reopen`,
  `milestone add|list|show`, `hook reseed --continue`, and `auto --off|--on`.
  Links to the changelog and license added; both were previously absent.
- **`specflo hook print` now says what its output is.** Alongside the JSON
  fragment (stdout, unchanged and still pipeable) it prints a two-line stderr
  note: this is Claude Code SessionStart wiring, a fragment to merge into
  `.claude/settings.json`, with `specflo hook install` as the safe way to do
  that; other agents/harnesses can adapt the wiring, while pi needs none (the
  bundled pi extension reseeds on its own). The bare JSON looked like a
  complete settings file, inviting a clobbering copy-paste. Both commands'
  help text now names Claude Code as the target harness.
- **`hook print --install` is deprecated** in favor of `specflo hook install`.
  It still works (same merge) but is hidden from `--help`.

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
- **`specflo status --json` reports an `auto_run` block.** `under_way` is true
  only while an auto run is genuinely live: the run-state file exists, the
  project is incomplete, the kill switch is clear, and no terminal stop marked
  the run ended. Every terminal stop -- kill switch, pass cap, stall, project
  completion -- now writes that end marker, so a finished run stops reading as a
  live one; the pass counter is preserved, so the cap still holds if the run
  resumes, and a fresh pass clears the marker. A project that never ran auto
  reports false and acquires no run-state file. The human `specflo status` block
  is unchanged.

- **`specflo extension install`.** Installs the bundled pi extension - the thin
  driver that reseeds a pi session from specflo - into pi's extension directory:
  `~/.pi/agent/extensions/specflo` by default, or `<cwd>/.pi/extensions/specflo`
  with `--scope project`. The extension source ships inside the specflo wheel as
  package data, so the install is a local copy plus a provenance stamp naming
  the specflo version that produced it - no network, no package manager, and no
  npm registry at any point. pi discovers both directories on its own, so
  nothing in `~/.pi/agent/settings.json` is read or written. Re-running is safe:
  an identical install is reported as already current, and a drifted or
  out-of-date one is replaced whole. There is no npm publication path - the
  extension's `package.json` is marked private and carries no publish config.

- **The pi extension reseeds a session on cold start.** When a pi session starts
  or resumes in a specflo repo, the extension runs `specflo hook reseed` and
  injects that stdout - byte for byte, with no prose of its own - into the first
  turn of the session, so a fresh pi already knows where the project stands. The
  ask-first payload is used, not the direct-continuation one: a cold start has
  nobody's answer yet about whether to keep going. It injects once per session,
  not once per turn. Outside a specflo repo, with no active project, or with
  `specflo` not installed at all, it injects nothing and the session is
  untouched. Sessions opened by a clear or a fork are left alone - those already
  know what they want next.

- **The pi extension arms on context usage.** At each turn's end the extension
  reads pi's own context-usage percent and arms its clear-and-continue trigger
  once that reaches the `context_threshold_percent` the CLI reports (default
  `75`). The check is an in-process read alone: it compares a percent of the
  window, never a raw token count, and spawns no subprocess of its own. Unknown
  usage never arms - an undefined reading, or the null percent compaction leaves
  behind, both read as unarmed. The threshold is seeded from the same cold-start
  status snapshot the extension already fetches, so no config file is opened and
  no extra process is paid. An armed turn takes the status poll a seam is read
  from.

- **The pi extension declares a seam while armed.** Each armed turn polls
  `specflo status --json` and declares a *seam* - a safe point to clear - when
  the phase differs from the last observed snapshot, or the done-task count has
  risen. A task merely moving to `in_progress` changes neither, so it declares
  nothing: clearing there would discard an in-flight task, which the trigger
  never does. The compared snapshot is the phase and done count alone, seeded
  from the same cold-start status the arming threshold rides on, so the seam
  check adds no state beyond the one snapshot and no process beyond the armed
  poll. A poll that returns nothing declares no seam and leaves the baseline
  intact. Acting on a declared seam is the notice's and the unattended
  fire's job, layered on top of the declaration.

- **The pi extension clears and reseeds on demand.** A `/specflo-continue`
  command clears the current pi session and reseeds the active project's
  direct-continuation payload into the replacement -- `specflo hook reseed
  --continue`'s stdout, byte for byte, run straight away rather than gated
  behind a confirmation, because invoking the command is that confirmation. The
  clear is the extension's own `ctx.newSession`, depending on no external
  clear-context package. Fetched before any clear, so with no active project the
  command clears nothing and says why through a notice that never reaches model
  context. Armed or not, the command is always available; acting on an armed
  seam automatically stays the notice's and the unattended fire's job; the
  `auto` argument continues an auto run instead, anchoring the chain.

- **The pi extension notices an armed seam in an attended run.** Outside an
  auto run, an armed seam now produces exactly one passive notice naming the
  current context usage, the seam that fired, and the `/specflo-continue`
  command to run - and nothing else: no dialog opens, no session is cleared,
  and the notice reaches the UI alone, never model context. Once per seam, not
  per turn - the compared snapshot advances with every armed poll, so an
  unchanged snapshot re-notifies nothing. A seam while `status --json` reports
  an auto run under way is the unattended fire's to deliver, not the notice's.
  Anything short of an explicit under-way `true` reads as attended - the mode
  that only ever notifies.
- **The pi extension fires the unattended clear at an auto-run seam.** While
  `status --json` reports an auto run under way, an armed seam clears and
  reseeds with no user input: the extension runs `specflo auto --json`, and a
  continuable pass opens a fresh session carrying that pass's payload verbatim.
  Loop control never leaves the CLI - on a stop verdict (kill switch, pass cap,
  stall, or project completion) nothing clears, no further pass starts, and the
  CLI's own stop directive reaches the user as a notice; the extension holds no
  pass counter and no cap. The fire runs through a live anchor - the
  replacement-session context captured at the previous clear - because pi gives
  extension event handlers no session control of their own: each clear anchors
  the next, `/specflo-continue auto` (the new command argument, the same
  explicit opt-in `specflo auto` is) starts or continues a run and anchors the
  chain by hand, and an unanchored armed auto seam (pi joined the run cold, or
  the anchor went stale) degrades to one bootstrap notice naming exactly that
  command. Arming state now re-seeds on every session start, so the loop
  re-arms across its own clears; the cold-start payload still injects only on
  startup and resume.

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
