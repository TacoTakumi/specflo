# The specflo model bench

The model bench measures how well a local model drives specflo. It seeds a
specflo project in a small Python repo, runs it unattended under `specflo auto`
with a model served by [llama-swap](https://github.com/mostlygeek/llama-swap),
and grades the result with a held-out test suite the agent never sees. Each
comparison changes one variable: the model, the engine or the harness (pi or
Claude Code). The output is a run record per run and graphs of held-out pass
rates with behaviour metrics and run-to-run spread.

The bench ships in the `specflo` package as `specflo bench`. It runs against
the pi, Claude Code and llama-swap installed on the machine, and each run
record names the version of each.

## Setup

```bash
pip install 'specflo[bench]'   # or: uv sync --extra bench
specflo bench init
```

`specflo bench init` writes the config templates into the bench home,
`<daemon root>/bench`, and keeps any file that is already there. The daemon
root is found as `specflo serve` finds it: `--root`, then
`SPECFLO_DAEMON_ROOT`, then the root saved in `~/.specflo/settings.yaml`. On
a first run with none of them set, the command asks for the root (default
`~/specflo-daemon`) and saves it. `specflo serve root <dir>` sets it ahead of
time.

Then name your llama-swap entries in `<home>/arms.yaml` and give each one a
model spec in `<home>/pi-models.json` (see Configuration).

Output from a bench that ran from a specflo checkout (`bench/runs`,
`bench/results`, `bench/state`) can move into the home as it is:

```bash
mv bench/runs bench/results bench/state "$(specflo serve root)/bench/"
```

## How a run works

A run is one arm, one level and one run index. An arm is one llama-swap entry
and one harness. A level is the specflo level of the seeded project: `quick`,
`fast` or `full`.

1. Preflight. The runner reads llama-swap's running list. When a model the
   bench did not load is loaded, it refuses the run and unloads nothing. When
   the arm's entry is not loaded, the runner loads it and records that in
   `<home>/state/loaded-by-bench.json`.
2. Sealed workdir. The fixture (tinytodo, a small to-do CLI shipped in the
   package) is copied to a new directory outside the bench home and the
   package, with a one-commit git history. A specflo project is seeded at the
   level, with the level's shipped request.
3. Harness run. The harness runs `specflo auto` (autonomy `autonomous`, pass
   cap 30) against the entry, with no human input. The pi arm uses the frozen
   pi config shipped in the package (settings, the specflo extension and a
   command deny list) with the model specs from `<home>/pi-models.json`. The
   Claude Code arm uses its own frozen config and continues across specflo
   context clears on its own.
4. End. The run ends for one reason: `complete`, `escalated`, `timeout` (the
   level's wall-clock cap), `stalled` (no model activity for the stall limit)
   or `harness-exit`.
5. Scoring. The session logs are normalised, then the runner computes
   behaviour metrics (turns, output tokens per turn, tool calls, tool errors,
   wall time), diagnostics flags, model-id validity and server telemetry. The
   grader copies the final tree into a separate grading directory, adds the
   level's held-out tests from the shipped archive and runs them with its own
   pytest config. The score is passed held-out tests over total held-out
   tests.
6. Record. `record.json` is checked against the record schema and written.

A run is invalid when a request named a model id other than the arm's entry,
or the tree could not be graded. A run is contaminated when a tool call named
the installed bench package, the held-out archive, the bench home, or another
run's workdir or run dir. Invalid and contaminated runs are left out of every
pass rate and graph, and the graph disclosure counts them.

## Requirements

- llama-swap at `http://localhost:8080` (override with `--base-url`), with the
  entries named in `<home>/arms.yaml`.
- At least one harness:
  - pi, for the pi arm. Any installed version runs; the record names it.
  - Claude Code, for the Claude Code arm. The bench runs the first `claude`
    on PATH that is not a script, so a wrapper script earlier on PATH is
    passed over. `SPECFLO_BENCH_CLAUDE` names another binary.
- herdr, to watch a run in a pane. Pass `--headless` to run without it.
- The llama-swap log at `~/AI/Engines/logs/llama-swap.log` (override with
  `--llama-swap-log`). Server telemetry needs an ISO-8601 time stamp at the
  start of each log line. Without it, the record says telemetry unavailable.
- The `bench` extra (matplotlib), for graphs only.

## Configuration

The bench home holds the operator's configs. `specflo bench init` writes each
one from its shipped template.

`<home>/arms.yaml` is the arm config. A run that names anything not in it is
refused before any request reaches llama-swap.

- `entries` - the llama-swap entry ids the bench may use, each with its engine.
- `harnesses` - `pi` and `claude-code`.
- `levels` - per-level `wall_clock` and `stall_limit` in seconds.
- `engines` - per-engine settings. `limit_factor` scales the level limits for
  that engine's entries.

`<home>/pi-models.json` is pi's model config for the `llama-swap` provider:
one model per entry, with the entry id as the model id, its context window
and its reasoning settings. The pi arm declares only the run's entry, and the
Claude Code arm reads the entry's context window from here too.

`<home>/matrices.yaml` names batch matrices. A matrix lists entries,
harnesses, levels and the number of valid runs each cell needs. A cell is one
entry, one harness and one level.

## Commands

Every command takes its configs, state and runs directory from the bench
home. A flag on the command line still wins. `specflo bench <command> --help`
lists a command's flags.

### Run one arm

```bash
specflo bench run --entry <entry> --harness pi|claude-code --level quick|fast|full [--headless]
```

Without `--run-index`, the run takes the lowest free index of the arm and
level. `--wall-clock` and `--stall-limit` override the level limits. The
command prints the record path and a summary. Exit 0 once the record is
written, whatever the end reason. Exit 1 when preflight or the harness start
refuses the run. Exit 2 for a bad arm, level or index.

### Run a matrix

```bash
specflo bench batch --matrix pilot --dry-run   # list the blocks, probes and runs
specflo bench batch --matrix pilot             # run them
```

The batch runs one block per entry, so llama-swap loads each entry once. It
starts only the runs each cell still needs, so a rerun resumes an interrupted
batch. It starts at most one full-level Claude Code run per night (12:00 to
12:00 local time). A run the night rule skips waits for a rerun. Before a
harness's runs on an entry, the batch probes the reasoning setting the engine
gets. When it differs from the same harness on another entry, the batch does
not start that harness's runs.

### Check the records

```bash
specflo bench records                          # one line per run dir
specflo bench records --check --matrix pilot   # valid count per cell
```

`--check --matrix` exits 1 when a cell is short or holds an invalid record. An
invalid record keeps the check failing until you move its run dir out of the
runs dir.

### Probe the reasoning setting

```bash
specflo bench probe --harness pi --harness claude-code
```

For each entry and harness, the probe sends one short prompt through a
capturing proxy and prints what the request carried, what the engine renders
with (reasoning on or off, effort) and what the reply shows. Exit 1 when a
harness gets a different setting on two engines.

### Grade a tree

```bash
specflo bench grade --tree <final tree> --level quick
```

Prints the grade as JSON. The runner calls the grader itself. Use this to
re-grade a tree by hand.

### Draw graphs

```bash
specflo bench graph pilot \
  --arm swift15-flash-next-iq4xs-strata-2x3090:pi \
  --arm swift15-flash-next-iq4xs-strata-2x3090:claude-code
```

The first argument names the results directory, `<home>/results/<name>`
(default `latest`); `--out` writes somewhere else. The graphs read the run
records in `<home>/runs`.

Writes a pass-rate graph and a metrics graph per level, each a PNG with its
disclosure under the plot and in a `.txt` beside it, and an `index.html` in
the output directory. The page shows every graph in that directory with its
arms as a table, and explains the bench, how to read a graph, each level and
each metric. The arms of a comparison may differ in one variable only: the
entry (with its engine, build and GGUF) or the harness (with its version).
Any other difference, such as a setting or the specflo version, is refused
with exit 1 and nothing is written. Without matplotlib, the command fails and
names the `bench` extra.

## Where output goes

```
<home>/runs/<entry>--<harness>--<level>--<index>/   run record, lifecycle, harness output, logs
<home>/runs/probes/<entry>--<harness>/probe.json    the batch's reasoning probes
<home>/state/loaded-by-bench.json                   entries the bench loaded
<home>/results/<name>/                              graphs
<temp dir>/modelbench/<run name>/                   sealed workdir and final tree
```

`<home>` is `<daemon root>/bench`. The record's `logs` field names every file
of a run.

## The held-out archive

Held-out tests and reference solutions exist only inside
`src/specflo/bench/data/heldout.tar.gz`, so no run workdir can read them as
plain text. To edit them in a specflo checkout, unpack, change and pack again:

```bash
uv run python -m specflo.bench.heldout unpack src/specflo/bench/data/heldout.tar.gz /tmp/h
uv run python -m specflo.bench.heldout pack /tmp/h src/specflo/bench/data/heldout.tar.gz
uv run python -m specflo.bench.heldout list src/specflo/bench/data/heldout.tar.gz
```

Each held-out suite must fail on the clean fixture and pass in full on its
reference solution. `tests/bench/test_grader.py` checks this.

## Tests

```bash
uv run --extra bench pytest tests/bench          # unit tests, no rig needed
uv run --extra bench pytest -m rig tests/bench   # live tests against real pi, herdr and llama-swap
```
