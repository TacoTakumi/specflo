# The specflo model bench

The model bench measures how well a local model drives specflo. It seeds a
specflo project in a small Python repo, runs it unattended under `specflo auto`
with a model served by [llama-swap](https://github.com/mostlygeek/llama-swap),
and grades the result with a held-out test suite the agent never sees. Each
comparison changes one variable: the model, the engine or the harness (pi or
Claude Code). The output is a run record per run and graphs of held-out pass
rates with behaviour metrics and run-to-run spread.

The bench lives in `bench/` and is a development tool. It is not part of the
installed `specflo` package.

## How a run works

A run is one arm, one level and one run index. An arm is one llama-swap entry
and one harness. A level is the specflo level of the seeded project: `quick`,
`fast` or `full`.

1. Preflight. The runner reads llama-swap's running list. When a model the
   bench did not load is loaded, it refuses the run and unloads nothing. When
   the arm's entry is not loaded, the runner loads it and records that in
   `bench/state/loaded-by-bench.json`.
2. Sealed workdir. The fixture (`bench/fixture`, a small to-do CLI called
   tinytodo) is copied to a new directory outside the repo with a one-commit
   git history. A specflo project is seeded at the level, with the level's
   request from `bench/fixture-levels/<level>.md`.
3. Harness run. The harness runs `specflo auto` (autonomy `autonomous`, pass
   cap 30) against the entry, with no human input. The pi arm uses the frozen
   config in `bench/configs/pi` (pinned pi version, specflo extension and a
   command deny list). The Claude Code arm uses `bench/configs/claude` and
   continues across specflo context clears on its own.
4. End. The run ends for one reason: `complete`, `escalated`, `timeout` (the
   level's wall-clock cap), `stalled` (no model activity for the stall limit)
   or `harness-exit`.
5. Scoring. The session logs are normalised, then the runner computes
   behaviour metrics (turns, output tokens per turn, tool calls, tool errors,
   wall time), diagnostics flags, model-id validity and server telemetry. The
   grader copies the final tree into a separate grading directory, adds the
   level's held-out tests from `bench/heldout.tar.gz` and runs them with its
   own pytest config. The score is passed held-out tests over total held-out
   tests.
6. Record. `record.json` is checked against the record schema and written.

A run is invalid when a request named a model id other than the arm's entry,
or the tree could not be graded. A run is contaminated when a tool call named
the bench directory or the held-out archive. Invalid and contaminated runs are
left out of every pass rate and graph, and the graph disclosure counts them.

## Requirements

- llama-swap at `http://localhost:8080` (override with `--base-url`), with the
  entries named in `bench/arms.yaml`.
- pi at the version pinned in `bench/configs/pi/frozen.json`, for the pi arm.
- Claude Code at the path in `bench/configs/claude/frozen.json`, for the
  Claude Code arm.
- herdr, to watch a run in a pane. Pass `--headless` to run without it.
- The llama-swap log at `~/AI/Engines/logs/llama-swap.log` (override with
  `--llama-swap-log`). Server telemetry needs an ISO-8601 time stamp at the
  start of each log line. Without it, the record says telemetry unavailable.
- The `bench` dependency group (matplotlib), for graphs only:
  `uv run --group bench ...`.

## Configuration

`bench/arms.yaml` is the arm config. A run that names anything not in it is
refused before any request reaches llama-swap.

- `entries` - the llama-swap entry ids the bench may use, each with its engine.
- `harnesses` - `pi` and `claude-code`.
- `levels` - per-level `wall_clock` and `stall_limit` in seconds.
- `engines` - per-engine settings. `limit_factor` scales the level limits for
  that engine's entries.

`bench/matrices.yaml` names batch matrices. A matrix lists entries, harnesses,
levels and the number of valid runs each cell needs. A cell is one entry, one
harness and one level.

## Commands

All commands go through `bench/mb.py`. Run them from the repo root.

### Run one arm

```bash
uv run bench/mb.py run --entry <entry> --harness pi|claude-code --level quick|fast|full [--headless]
```

Without `--run-index`, the run takes the lowest free index of the arm and
level. `--wall-clock` and `--stall-limit` override the level limits. The
command prints the record path and a summary. Exit 0 once the record is
written, whatever the end reason. Exit 1 when preflight or the harness start
refuses the run. Exit 2 for a bad arm, level or index.

### Run a matrix

```bash
uv run bench/mb.py batch --matrix pilot --dry-run   # list the blocks, probes and runs
uv run bench/mb.py batch --matrix pilot             # run them
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
uv run bench/mb.py records                          # one line per run dir
uv run bench/mb.py records --check --matrix pilot   # valid count per cell
```

`--check --matrix` exits 1 when a cell is short or holds an invalid record. An
invalid record keeps the check failing until you move its run dir out of the
runs dir.

### Probe the reasoning setting

```bash
uv run bench/mb.py probe --harness pi --harness claude-code
```

For each entry and harness, the probe sends one short prompt through a
capturing proxy and prints what the request carried, what the engine renders
with (reasoning on or off, effort) and what the reply shows. Exit 1 when a
harness gets a different setting on two engines.

### Grade a tree

```bash
uv run bench/mb.py grade --tree <final tree> --level quick
```

Prints the grade as JSON. The runner calls the grader itself. Use this to
re-grade a tree by hand.

### Draw graphs

```bash
uv run --group bench bench/mb.py graph --records bench/runs --out bench/results/pilot \
  --arm swift15-flash-next-iq4xs-strata-2x3090:pi \
  --arm swift15-flash-next-iq4xs-strata-2x3090:claude-code
```

Writes a pass-rate graph and a metrics graph per level, each a PNG with its
disclosure under the plot and in a `.txt` beside it. The arms of a comparison
may differ in one variable only: the entry (with its engine, build and GGUF)
or the harness (with its version). Any other difference, such as a setting or
the specflo version, is refused with exit 1 and nothing is written.

## Where output goes

```
bench/runs/<entry>--<harness>--<level>--<index>/   run record, lifecycle, harness output, logs
bench/runs/probes/<entry>--<harness>/probe.json    the batch's reasoning probes
bench/state/loaded-by-bench.json                   entries the bench loaded
bench/results/                                     graphs
<temp dir>/modelbench/<run name>/                  sealed workdir and final tree
```

`bench/runs/`, `bench/state/` and `bench/results/` are gitignored. The
record's `logs` field names every file of a run.

## The held-out archive

Held-out tests and reference solutions exist only inside
`bench/heldout.tar.gz`, so no run workdir can read them as plain text. To edit
them, unpack, change and pack again:

```bash
uv run python bench/modelbench/heldout.py unpack bench/heldout.tar.gz /tmp/h
uv run python bench/modelbench/heldout.py pack /tmp/h bench/heldout.tar.gz
uv run python bench/modelbench/heldout.py list bench/heldout.tar.gz
```

Each held-out suite must fail on the clean fixture and pass in full on its
reference solution. `tests/bench/test_grader.py` checks this.

## Tests

```bash
uv run pytest tests/bench              # unit tests, no rig needed
uv run pytest -m rig tests/bench       # live tests against real pi, herdr and llama-swap
```
