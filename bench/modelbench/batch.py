"""Batch runner: a matrix of arms, levels and run counts, in entry blocks, with resume and the night rule.

A matrix (bench/matrices.yaml) names llama-swap entries, harnesses, levels and
the number of runs each cell needs. A cell is one entry, one harness and one
level. The batch runs one block per entry, in the matrix's entry order, so
llama-swap loads each entry once.

Counting: a cell is full when it holds `runs` valid run records. A run record
counts when <runs dir>/<run name>/record.json exists, passes the schema, has
`valid: true` and names the arm, level and index of its directory. Every
other record is invalid: it does not count, and the batch adds a new run
index to the cell in its place. Any valid record of the cell counts, whichever
batch (or single `mb.py run`) wrote it.

Resume: the plan is made from the records on disk, so a rerun after an
interruption starts only the runs the cells still lack. A run killed mid-way
leaves a run dir without record.json. It is left as it is (its logs may tell
why it stopped) and a new run takes the next free index (`run.next_run_index`).

Order in a block: by round, then level, then harness. Round k holds the k-th
missing run of each cell, so each round ends with the full-level Claude Code
run. A night-rule skip does not stop the block, and a block that runs into a
new night meets another full-level Claude Code slot in its next round.

Night rule: at most one full-level Claude Code run starts per night, over all
entries. A night is the 24 hours from NIGHT_START_HOUR (12:00) local time to
12:00 the next day, named by the date it starts on, so an overnight batch is
in one night. A full-level Claude Code run has started in a night when a run
record of harness claude-code, level full has its `started_at` in it (valid
or not), when a run dir of that cell has no record and was last changed in it
(an interrupted run), or when this batch started one in it. A run the rule
skips is reported and left for a rerun on a later night.

Reasoning settings: before a harness's runs in a block, the batch probes that
harness on the block's entry (`probe.probe`) and stores the result as
<runs dir>/probes/<entry>--<harness>/probe.json. When a stored probe of the
same harness on another entry of the matrix exists, `probe.require_same`
compares the two; on a ProbeRefusal the harness's runs in this block are not
started (the other harness's runs go on) and the batch exits nonzero.

A run that fails to start or finish (preflight refusal, harness start, rig
error) stops the batch; a rerun resumes it.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from modelbench import arms, launch_pi, lifecycle, preflight, probe, record, run
from modelbench import workdir as wd

DEFAULT_MATRICES = Path(__file__).resolve().parents[1] / "matrices.yaml"
PROBES_DIR_NAME = "probes"
PROBE_FILE = "probe.json"

NIGHT_START_HOUR = 12
NIGHT_HARNESS = "claude-code"
NIGHT_LEVEL = "full"

# Errors that stop the batch: the same ones `mb.py run` reports.
STOP_ERRORS = (arms.ArmError, run.RunError, preflight.PreflightError, probe.ProbeError,
               lifecycle.LifecycleError, launch_pi.LaunchError, wd.WorkdirError, OSError)

RunFn = Callable[..., tuple[Path, dict[str, Any]]]
ProbeFn = Callable[[str, str], probe.ProbeResult]
Clock = Callable[[], datetime]


class MatrixError(ValueError):
    """A bad matrix file or matrix field. The message starts with the field."""


def local_now() -> datetime:
    return datetime.now().astimezone()


# -- matrices ---------------------------------------------------------------------


@dataclass(frozen=True)
class Matrix:
    name: str
    entries: tuple[str, ...]
    harnesses: tuple[str, ...]
    levels: tuple[str, ...]
    runs: int

    def cells(self) -> Iterator[tuple[str, str, str]]:
        for entry in self.entries:
            for harness in self.harnesses:
                for level in self.levels:
                    yield entry, harness, level

    @property
    def total(self) -> int:
        return len(self.entries) * len(self.harnesses) * len(self.levels) * self.runs


def _names(field_name: str, value: Any, known: Iterable[str]) -> tuple[str, ...]:
    known = tuple(known)
    if not isinstance(value, list) or not value:
        raise MatrixError(f"{field_name}: missing or empty list")
    for name in value:
        if name not in known:
            raise MatrixError(f"{field_name}: unknown {name!r} (known: {', '.join(known)})")
    if len(set(value)) != len(value):
        raise MatrixError(f"{field_name}: lists a name twice")
    return tuple(value)


def load_matrices(path: Path | str = DEFAULT_MATRICES) -> dict[str, dict[str, Any]]:
    """The raw matrices of a matrix file, by name."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    matrices = data.get("matrices") if isinstance(data, dict) else None
    if not isinstance(matrices, dict) or not matrices:
        raise MatrixError(f"matrices: missing or empty mapping in {path}")
    return matrices


def load_matrix(name: str, config: arms.Config, path: Path | str = DEFAULT_MATRICES) -> Matrix:
    """One matrix, checked against the arm config; raise MatrixError naming the bad field."""
    matrices = load_matrices(path)
    if name not in matrices:
        raise MatrixError(f"matrix: unknown {name!r} (known: {', '.join(matrices)})")
    spec = matrices[name]
    if not isinstance(spec, dict):
        raise MatrixError(f"matrices.{name}: expected a mapping")
    runs = spec.get("runs")
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
        raise MatrixError(f"matrices.{name}.runs: expected a positive int, got {runs!r}")
    return Matrix(
        name=name,
        entries=_names(f"matrices.{name}.entries", spec.get("entries"), config.entries),
        harnesses=_names(f"matrices.{name}.harnesses", spec.get("harnesses"), config.harnesses),
        levels=_names(f"matrices.{name}.levels", spec.get("levels"), config.levels),
        runs=runs,
    )


# -- run records on disk ------------------------------------------------------------


@dataclass(frozen=True)
class RunDir:
    """One run dir under the runs dir and what its record says."""

    name: str
    entry: str
    harness: str
    level: str
    run_index: int
    path: Path
    record: dict[str, Any] | None
    problems: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        if self.record is None and not self.problems:
            return "incomplete"
        return "invalid" if self.problems else "valid"

    @property
    def cell(self) -> tuple[str, str, str]:
        return self.entry, self.harness, self.level


def parse_run_name(name: str) -> tuple[str, str, str, int] | None:
    """(entry, harness, level, index) from a run dir name, or None when it is not one."""
    parts = name.rsplit("--", 3)
    if len(parts) != 4 or not parts[3].isdigit() or not parts[0]:
        return None
    entry, harness, level, index = parts
    if harness not in arms.HARNESSES or level not in arms.LEVELS:
        return None
    return entry, harness, level, int(index)


def record_problems(rec: Any, entry: str, harness: str, level: str, run_index: int) -> list[str]:
    """Why a run record does not count; empty when it is valid."""
    errors = record.validate_record(rec)
    if errors:
        return [f"schema: {e}" for e in errors]
    out = []
    if (rec["arm"]["entry"], rec["arm"]["harness"], rec["level"], rec["run_index"]) != (
            entry, harness, level, run_index):
        out.append("the record names another arm, level or index than its run dir")
    if rec["valid"] is not True:
        reasons = (rec.get("validity") or {}).get("reasons") or ["valid is false"]
        out.extend(str(r) for r in reasons)
        out.append(f"end reason {rec['end_reason']}")
    return out


def scan_runs(runs_dir: Path | str = run.RUNS_DIR) -> list[RunDir]:
    """Every run dir under the runs dir, by name; other dirs (probes) are left out."""
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    found = []
    for path in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        parsed = parse_run_name(path.name)
        if parsed is None:
            continue
        entry, harness, level, index = parsed
        rec_path = path / run.RECORD_FILE
        rec: dict[str, Any] | None = None
        problems: list[str] = []
        if rec_path.exists():
            try:
                loaded = json.loads(rec_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                problems = [f"unreadable record: {exc}"]
            else:
                rec = loaded if isinstance(loaded, dict) else None
                problems = record_problems(loaded, entry, harness, level, index)
        found.append(RunDir(path.name, entry, harness, level, index, path, rec, tuple(problems)))
    return found


def by_cell(found: Iterable[RunDir]) -> dict[tuple[str, str, str], list[RunDir]]:
    cells: dict[tuple[str, str, str], list[RunDir]] = {}
    for item in found:
        cells.setdefault(item.cell, []).append(item)
    return cells


# -- the night rule -------------------------------------------------------------------


def night_of(moment: datetime) -> datetime:
    """The start of the night `moment` is in: the last NIGHT_START_HOUR at or before it."""
    start = moment.replace(hour=NIGHT_START_HOUR, minute=0, second=0, microsecond=0)
    return start if moment >= start else start - timedelta(days=1)


def describe_night(start: datetime) -> str:
    end = start + timedelta(days=1)
    return f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}"


def night_runs(found: Iterable[RunDir]) -> list[tuple[str, datetime]]:
    """(run name, start) of every full-level Claude Code run on disk.

    The start is the record's `started_at`; for a run dir with no record (an
    interrupted run), the time the dir was last changed.
    """
    out = []
    for item in found:
        if item.harness != NIGHT_HARNESS or item.level != NIGHT_LEVEL:
            continue
        started = None
        if item.record is not None and isinstance(item.record.get("started_at"), str):
            try:
                started = datetime.fromisoformat(item.record["started_at"])
            except ValueError:
                started = None
        if started is None and item.record is None:
            try:
                started = datetime.fromtimestamp(item.path.stat().st_mtime).astimezone()
            except OSError:
                started = None
        if started is not None:
            if started.tzinfo is None:
                started = started.astimezone()
            out.append((item.name, started))
    return out


def night_blocker(starts: Iterable[tuple[str, datetime]], now: datetime) -> tuple[str, datetime] | None:
    """The full-level Claude Code run that already started in `now`'s night, or None."""
    night = night_of(now)
    for name, started in starts:
        local = started.astimezone(now.tzinfo)
        if night_of(local) == night:
            return name, local
    return None


# -- the plan ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Planned:
    """One run a cell still needs: its round (0-based slot in the cell) and the index it would take."""

    entry: str
    harness: str
    level: str
    round: int
    run_index: int

    @property
    def name(self) -> str:
        return run.run_name(arms.Run(self.entry, self.harness, self.level, self.run_index))

    @property
    def night_rule(self) -> bool:
        return self.harness == NIGHT_HARNESS and self.level == NIGHT_LEVEL


@dataclass
class Block:
    entry: str
    engine: str
    runs: list[Planned] = field(default_factory=list)
    recorded: int = 0

    @property
    def harnesses(self) -> list[str]:
        """The harnesses with runs to start in this block, in the block's order."""
        return list(dict.fromkeys(p.harness for p in self.runs))


@dataclass
class Plan:
    matrix: Matrix
    blocks: list[Block]
    cells: dict[tuple[str, str, str], list[RunDir]]

    @property
    def to_start(self) -> int:
        return sum(len(b.runs) for b in self.blocks)

    @property
    def recorded(self) -> int:
        return sum(b.recorded for b in self.blocks)


def free_indices(entry: str, harness: str, level: str, count: int, *,
                 runs_dir: Path | str, work_root: Path | str) -> list[int]:
    """The `count` lowest run indices of a cell whose run dir and workdir are both free."""
    out, index = [], 0
    while len(out) < count:
        name = run.run_name(arms.Run(entry, harness, level, index))
        if not (Path(runs_dir) / name).exists() and not (Path(work_root) / name).exists():
            out.append(index)
        index += 1
    return out


def make_plan(matrix: Matrix, config: arms.Config, *, runs_dir: Path | str = run.RUNS_DIR,
              work_root: Path | str = run.WORK_ROOT) -> Plan:
    """The runs each cell still needs, in entry blocks, ordered by round, level, harness."""
    cells = by_cell(scan_runs(runs_dir))
    blocks = []
    for entry in matrix.entries:
        block = Block(entry=entry, engine=str(config.entries[entry]["engine"]))
        for harness in matrix.harnesses:
            for level in matrix.levels:
                valid = sum(1 for r in cells.get((entry, harness, level), []) if r.state == "valid")
                have = min(valid, matrix.runs)
                block.recorded += have
                need = matrix.runs - have
                indices = free_indices(entry, harness, level, need, runs_dir=runs_dir, work_root=work_root)
                for slot, index in zip(range(have, matrix.runs), indices):
                    block.runs.append(Planned(entry, harness, level, slot, index))
        block.runs.sort(key=lambda p: (p.round, matrix.levels.index(p.level), matrix.harnesses.index(p.harness)))
        blocks.append(block)
    return Plan(matrix=matrix, blocks=blocks, cells=cells)


def describe_plan(plan: Plan, *, runs_dir: Path | str = run.RUNS_DIR, now: datetime | None = None) -> list[str]:
    """The dry-run listing: blocks, probe steps and runs, with the night rule's state now."""
    now = now or local_now()
    m = plan.matrix
    lines = [
        f"batch {m.name}: {len(plan.blocks)} entry block(s), {m.total} runs in the matrix "
        f"({len(m.entries)} entries x {len(m.harnesses)} harnesses x {len(m.levels)} levels x {m.runs}); "
        f"{plan.recorded} recorded valid, {plan.to_start} to start",
        f"night rule: at most one full-level {NIGHT_HARNESS} run per night "
        f"(a night is {NIGHT_START_HOUR:02d}:00 to {NIGHT_START_HOUR:02d}:00 local time)",
    ]
    blocker = night_blocker(night_runs(scan_runs(runs_dir)), now)
    night = describe_night(night_of(now))
    lines.append(f"this night ({night}): " + (
        f"{blocker[0]} started {blocker[1]:%Y-%m-%d %H:%M}, so no full-level {NIGHT_HARNESS} run starts now"
        if blocker else f"no full-level {NIGHT_HARNESS} run yet"))
    for n, block in enumerate(plan.blocks, 1):
        lines.append(f"block {n}: {block.entry} ({block.engine}): {len(block.runs)} to start, "
                     f"{block.recorded} recorded valid")
        others = [e for e in m.entries if e != block.entry]
        for harness in block.harnesses:
            compare = f"; compare with the stored {harness} probe of {', '.join(others)}" if others else ""
            lines.append(f"  probe {harness} on {block.entry}{compare}")
        for p in block.runs:
            note = "  [night rule]" if p.night_rule else ""
            lines.append(f"  run {p.name} (round {p.round + 1}/{m.runs}){note}")
    return lines


# -- probes ------------------------------------------------------------------------------


def probe_path(probes_dir: Path | str, entry: str, harness: str) -> Path:
    return Path(probes_dir) / f"{entry}--{harness}" / PROBE_FILE


def save_probe(probes_dir: Path | str, result: probe.ProbeResult, now: datetime) -> Path:
    path = probe_path(probes_dir, result.entry, result.harness)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**result.to_dict(), "probed_at": now.isoformat(timespec="seconds")}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def load_probe(probes_dir: Path | str, entry: str, harness: str) -> probe.ProbeResult | None:
    path = probe_path(probes_dir, entry, harness)
    if not path.exists():
        return None
    try:
        return probe.ProbeResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise probe.ProbeError(f"cannot read the stored probe {path}: {exc}") from None


def rig_probe(rig: run.Rig, config: arms.Config, probes_dir: Path | str,
              timeout: float = probe.DEFAULT_TIMEOUT) -> ProbeFn:
    """The probe step on the rig: preflight, load the entry when needed, probe the harness.

    A probe dir left by an earlier batch is cleared first: every batch probes
    again, and the new probe replaces the stored one.
    """

    def probe_fn(entry: str, harness: str) -> probe.ProbeResult:
        run.ready_entry(rig, arms.Run(entry, harness, next(iter(config.levels)), 0))
        run_dir = probe_path(probes_dir, entry, harness).parent
        if run_dir.exists():
            shutil.rmtree(run_dir)
        return probe.probe(harness, entry, base_url=rig.base_url, timeout=timeout, config=config,
                           run_dir=run_dir)

    return probe_fn


# -- the batch ---------------------------------------------------------------------------


@dataclass
class Outcome:
    started: list[str] = field(default_factory=list)
    night_skipped: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    stopped: str | None = None

    @property
    def exit_code(self) -> int:
        """0 when every run allowed tonight ran; 1 when the batch stopped or a probe refused."""
        return 1 if self.stopped or self.refused else 0


def run_batch(
    plan: Plan,
    *,
    config: arms.Config,
    run_fn: RunFn,
    probe_fn: ProbeFn,
    runs_dir: Path | str = run.RUNS_DIR,
    work_root: Path | str = run.WORK_ROOT,
    clock: Clock = local_now,
    out: Callable[[str], None] = print,
) -> Outcome:
    """Run the plan block by block; see the module doc for the rules.

    `run_fn(entry=, harness=, level=, run_index=)` runs one run to its record
    (`run.run_one` with the bench-wide settings); `probe_fn(entry, harness)`
    probes one harness on one entry.
    """
    runs_dir = Path(runs_dir)
    probes_dir = runs_dir / PROBES_DIR_NAME
    result = Outcome()
    session_night: list[tuple[str, datetime]] = []
    matrix = plan.matrix
    for n, block in enumerate(plan.blocks, 1):
        out(f"block {n}: {block.entry} ({block.engine}): {len(block.runs)} to start")
        refused: set[str] = set()
        for harness in block.harnesses:
            try:
                found = probe_fn(block.entry, harness)
                save_probe(probes_dir, found, clock())
                out(f"  probe {harness} on {block.entry}: reasoning "
                    f"{'on' if found.received.thinking else 'off'}"
                    + (f", effort {found.received.effort}" if found.received.thinking else ""))
                for other in matrix.entries:
                    if other == block.entry:
                        continue
                    stored = load_probe(probes_dir, other, harness)
                    if stored is None:
                        continue
                    first, second = ((stored, found) if matrix.entries.index(other) < matrix.entries.index(block.entry)
                                     else (found, stored))
                    probe.require_same(first, second)
            except probe.ProbeRefusal as exc:
                refused.add(harness)
                out(f"  refused: {exc}; the {harness} runs of this block are not started")
            except STOP_ERRORS as exc:
                result.stopped = f"probe {harness} on {block.entry}: {exc}"
                out(f"  stopped: {result.stopped}")
                out("rerun the batch to resume")
                return result
        for p in block.runs:
            if p.harness in refused:
                result.refused.append(p.name)
                continue
            if p.night_rule:
                now = clock()
                blocker = night_blocker(night_runs(scan_runs(runs_dir)) + session_night, now)
                if blocker:
                    result.night_skipped.append(p.name)
                    out(f"  skip {p.entry} / {p.harness} / {p.level} (round {p.round + 1}): a full-level "
                        f"{NIGHT_HARNESS} run already started this night ({blocker[0]} at "
                        f"{blocker[1]:%Y-%m-%d %H:%M}; night {describe_night(night_of(now))}); "
                        "rerun the batch on a later night")
                    continue
            try:
                index = run.next_run_index(config, entry=p.entry, harness=p.harness, level=p.level,
                                           runs_dir=runs_dir, work_root=work_root)
                name = run.run_name(arms.Run(p.entry, p.harness, p.level, index))
                if p.night_rule:
                    session_night.append((name, clock()))
                out(f"  start {name} (round {p.round + 1}/{matrix.runs})")
                result.started.append(name)
                _, rec = run_fn(entry=p.entry, harness=p.harness, level=p.level, run_index=index)
            except STOP_ERRORS as exc:
                result.stopped = f"{p.entry} / {p.harness} / {p.level}: {exc}"
                out(f"  stopped: {result.stopped}")
                out("rerun the batch to resume")
                return result
            out(f"  done {name}: {rec.get('end_reason')}, valid {rec.get('valid')}, "
                f"score {float(rec.get('score') or 0.0):.3f}")
    return result


def summary(outcome: Outcome, plan: Plan, runs_dir: Path | str = run.RUNS_DIR) -> list[str]:
    """The closing lines: what ran, what was left, and the cells still short."""
    lines = [f"batch {plan.matrix.name}: {len(outcome.started)} started, "
             f"{len(outcome.night_skipped)} left by the night rule, {len(outcome.refused)} refused"]
    short = [c for c in check_cells(plan.matrix, runs_dir) if c.valid < c.required]
    if short:
        lines.append(f"{len(short)} cell(s) still short; rerun the batch to fill them "
                     "(`mb.py records --check --matrix " + plan.matrix.name + "`)")
    return lines


# -- the check ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CellCheck:
    entry: str
    harness: str
    level: str
    required: int
    valid: int
    invalid: tuple[RunDir, ...]
    incomplete: tuple[RunDir, ...]

    @property
    def ok(self) -> bool:
        return self.valid >= self.required and not self.invalid


def check_cells(matrix: Matrix, runs_dir: Path | str = run.RUNS_DIR) -> list[CellCheck]:
    cells = by_cell(scan_runs(runs_dir))
    out = []
    for cell in matrix.cells():
        items = cells.get(cell, [])
        out.append(CellCheck(
            *cell, required=matrix.runs,
            valid=sum(1 for r in items if r.state == "valid"),
            invalid=tuple(r for r in items if r.state == "invalid"),
            incomplete=tuple(r for r in items if r.state == "incomplete"),
        ))
    return out
