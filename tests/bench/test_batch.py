"""The batch runner: entry blocks, resume, the night rule, the probe refusal and the records check.

Offline: the run and probe steps are stand-ins that write run records and
return probe results, the clock is fixed, and every runs dir is a temp dir.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modelbench import arms, batch, cmd_batch, cmd_records, probe, record, run

LLAMA = "swift15-flash-next-iq4xs-mtp-vision"
STRATA = "swift15-flash-next-iq4xs-strata-2x3090"
TZ = timezone(timedelta(hours=-3))
EVENING = datetime(2026, 10, 8, 21, 0, tzinfo=TZ)


@pytest.fixture
def config() -> arms.Config:
    return arms.load_config()


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "runs", tmp_path / "work"


def make_record(entry: str, harness: str, level: str, index: int, *, started: datetime = EVENING,
                valid: bool = True) -> dict:
    rec = {
        "arm": {"entry": entry, "harness": harness},
        "level": level,
        "run_index": index,
        "versions": {"harness": "h", "engine_build": "b", "specflo": "0", "entry": entry, "gguf_path": "/m.gguf"},
        "settings": {"sampling": {}, "context_window": 1, "autonomy": run.AUTONOMY, "pass_cap": run.PASS_CAP},
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(minutes=5)).isoformat(),
        "end_reason": "complete" if valid else "harness-exit",
        "valid": valid,
        "validity": {"reasons": [] if valid else ["model ids [] are not exactly the entry"]},
        "score": 0.5,
        "metrics": {}, "diagnostics": {}, "telemetry": {}, "logs": {},
        "lifecycle": {"placement": "headless", "end_detail": "x", "wall_clock_cap": 1.0, "stall_limit": 1.0},
    }
    assert record.validate_record(rec) == []
    return rec


def write_record(runs_dir: Path, rec: dict) -> Path:
    name = run.run_name(arms.Run(rec["arm"]["entry"], rec["arm"]["harness"], rec["level"], rec["run_index"]))
    path = runs_dir / name / run.RECORD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec), encoding="utf-8")
    return path


class FakeRunner:
    """Writes a valid record per run, started at the clock; raises KeyboardInterrupt on call `stop_at`."""

    def __init__(self, runs_dir: Path, clock, stop_at: int | None = None) -> None:
        self.runs_dir, self.clock, self.stop_at = runs_dir, clock, stop_at
        self.calls: list[str] = []

    def __call__(self, *, entry, harness, level, run_index):
        name = run.run_name(arms.Run(entry, harness, level, run_index))
        self.calls.append(name)
        (self.runs_dir / name).mkdir(parents=True)
        if self.stop_at is not None and len(self.calls) == self.stop_at:
            raise KeyboardInterrupt
        rec = make_record(entry, harness, level, run_index, started=self.clock())
        return write_record(self.runs_dir, rec), rec


def fake_probe(effort_by_entry: dict[tuple[str, str], str] | None = None):
    efforts = effort_by_entry or {}
    calls: list[tuple[str, str]] = []

    def probe_fn(entry: str, harness: str) -> probe.ProbeResult:
        calls.append((entry, harness))
        effort = efforts.get((entry, harness), "xhigh")
        return probe.ProbeResult(
            harness=harness, entry=entry, engine="strata" if entry == STRATA else "llama.cpp",
            api=probe.API_OF_HARNESS[harness], sent={},
            received=probe.Received(thinking=True, effort=effort, source="template default"),
            reasoning_chars=40)

    probe_fn.calls = calls
    return probe_fn


def go(plan, config, dirs, *, runner, probe_fn, clock=lambda: EVENING):
    lines: list[str] = []
    outcome = batch.run_batch(plan, config=config, run_fn=runner, probe_fn=probe_fn, runs_dir=dirs[0],
                              work_root=dirs[1], clock=clock, out=lines.append)
    return outcome, lines


# -- matrices and the plan ------------------------------------------------------------


def test_first_round_dry_run_lists_two_entry_blocks_and_sixty_runs(config, dirs):
    matrix = batch.load_matrix("first-round", config)
    plan = batch.make_plan(matrix, config, runs_dir=dirs[0], work_root=dirs[1])
    assert [b.entry for b in plan.blocks] == [LLAMA, STRATA]
    assert plan.to_start == 60 and matrix.total == 60
    for block in plan.blocks:
        assert {p.entry for p in block.runs} == {block.entry}
        assert len(block.runs) == 30
    lines = batch.describe_plan(plan, runs_dir=dirs[0], now=EVENING)
    assert sum(1 for line in lines if line.startswith("block ")) == 2
    assert sum(1 for line in lines if line.startswith("  run ")) == 60
    assert sum(1 for line in lines if line.startswith("  probe ")) == 4


def test_pilot_is_one_block_of_six_runs(config, dirs):
    plan = batch.make_plan(batch.load_matrix("pilot", config), config, runs_dir=dirs[0], work_root=dirs[1])
    assert len(plan.blocks) == 1 and plan.blocks[0].entry == LLAMA
    assert [(p.harness, p.level) for p in plan.blocks[0].runs] == [
        ("pi", "quick"), ("claude-code", "quick"), ("pi", "fast"), ("claude-code", "fast"),
        ("pi", "full"), ("claude-code", "full")]


def test_each_round_ends_with_the_full_level_claude_code_run(config, dirs):
    plan = batch.make_plan(batch.load_matrix("first-round", config), config, runs_dir=dirs[0], work_root=dirs[1])
    runs = plan.blocks[0].runs
    rounds = [runs[i:i + 6] for i in range(0, 30, 6)]
    assert all(r[-1].night_rule and not any(p.night_rule for p in r[:-1]) for r in rounds)
    assert [r[0].round for r in rounds] == [0, 1, 2, 3, 4]


def test_cli_dry_run_prints_the_plan_and_starts_nothing(dirs, capsys):
    code = cmd_batch.main(["--matrix", "pilot", "--dry-run", "--runs-dir", str(dirs[0]),
                           "--work-root", str(dirs[1])])
    out = capsys.readouterr().out
    assert code == 0
    assert "1 entry block(s)" in out and out.count("  run ") == 6
    assert not dirs[0].exists()


def test_unknown_matrix_and_bad_field_are_refused(config, tmp_path, dirs, capsys):
    assert cmd_batch.main(["--matrix", "nope", "--dry-run", "--runs-dir", str(dirs[0])]) == 2
    assert "unknown 'nope'" in capsys.readouterr().err
    bad = tmp_path / "m.yaml"
    bad.write_text("matrices:\n  m:\n    entries: [not-an-entry]\n    harnesses: [pi]\n"
                   "    levels: [quick]\n    runs: 1\n")
    with pytest.raises(batch.MatrixError, match=r"matrices\.m\.entries: unknown 'not-an-entry'"):
        batch.load_matrix("m", config, bad)


def test_valid_records_fill_cells_and_invalid_ones_do_not(config, dirs):
    write_record(dirs[0], make_record(LLAMA, "pi", "quick", 0))
    write_record(dirs[0], make_record(LLAMA, "claude-code", "quick", 0, valid=False))
    plan = batch.make_plan(batch.load_matrix("pilot", config), config, runs_dir=dirs[0], work_root=dirs[1])
    names = [p.name for p in plan.blocks[0].runs]
    assert f"{LLAMA}--pi--quick--000" not in names and not any("--pi--quick--" in n for n in names)
    # the invalid record's cell gets a new run at the next index
    assert f"{LLAMA}--claude-code--quick--001" in names
    assert plan.recorded == 1 and plan.to_start == 5


# -- resume ---------------------------------------------------------------------------


def test_rerun_after_an_interruption_starts_only_the_missing_runs(config, dirs):
    matrix = batch.load_matrix("pilot", config)
    plan = batch.make_plan(matrix, config, runs_dir=dirs[0], work_root=dirs[1])
    first = FakeRunner(dirs[0], lambda: EVENING, stop_at=3)
    with pytest.raises(KeyboardInterrupt):
        go(plan, config, dirs, runner=first, probe_fn=fake_probe())
    assert len(first.calls) == 3
    interrupted = dirs[0] / first.calls[2]
    assert interrupted.is_dir() and not (interrupted / run.RECORD_FILE).exists()

    plan = batch.make_plan(matrix, config, runs_dir=dirs[0], work_root=dirs[1])
    second = FakeRunner(dirs[0], lambda: EVENING)
    outcome, _ = go(plan, config, dirs, runner=second, probe_fn=fake_probe())
    assert outcome.exit_code == 0
    assert not set(second.calls) & set(first.calls[:2])
    # the interrupted run's dir is left alone; its cell takes the next index
    assert f"{LLAMA}--pi--fast--001" in second.calls and interrupted.is_dir()
    assert len(second.calls) == 4
    assert all(c.ok for c in batch.check_cells(matrix, dirs[0]))

    third = FakeRunner(dirs[0], lambda: EVENING)
    plan = batch.make_plan(matrix, config, runs_dir=dirs[0], work_root=dirs[1])
    outcome, _ = go(plan, config, dirs, runner=third, probe_fn=fake_probe())
    assert third.calls == [] and plan.to_start == 0


def test_a_run_error_stops_the_batch_and_a_rerun_resumes(config, dirs):
    matrix = batch.load_matrix("pilot", config)
    ok = FakeRunner(dirs[0], lambda: EVENING)

    def failing(**kw):
        if kw["level"] == "fast":
            raise run.RunError("harness would not start")
        return ok(**kw)

    outcome, lines = go(batch.make_plan(matrix, config, runs_dir=dirs[0], work_root=dirs[1]), config, dirs,
                        runner=failing, probe_fn=fake_probe())
    assert outcome.exit_code == 1 and "harness would not start" in outcome.stopped
    assert len(ok.calls) == 2 and "rerun the batch to resume" in lines
    again = FakeRunner(dirs[0], lambda: EVENING)
    go(batch.make_plan(matrix, config, runs_dir=dirs[0], work_root=dirs[1]), config, dirs,
       runner=again, probe_fn=fake_probe())
    assert [c.split("--")[-2] for c in again.calls] == ["fast", "fast", "full", "full"]


# -- the night rule ---------------------------------------------------------------------


def test_night_runs_from_noon_to_noon():
    assert batch.night_of(datetime(2026, 10, 8, 21, tzinfo=TZ)) == datetime(2026, 10, 8, 12, tzinfo=TZ)
    assert batch.night_of(datetime(2026, 10, 9, 11, 59, tzinfo=TZ)) == datetime(2026, 10, 8, 12, tzinfo=TZ)
    assert batch.night_of(datetime(2026, 10, 9, 12, tzinfo=TZ)) == datetime(2026, 10, 9, 12, tzinfo=TZ)


def test_a_second_full_level_claude_code_run_in_one_night_is_skipped(config, dirs):
    write_record(dirs[0], make_record(STRATA, "claude-code", "full", 0, started=EVENING - timedelta(hours=2)))
    plan = batch.make_plan(batch.load_matrix("pilot", config), config, runs_dir=dirs[0], work_root=dirs[1])
    runner = FakeRunner(dirs[0], lambda: EVENING)
    outcome, lines = go(plan, config, dirs, runner=runner, probe_fn=fake_probe())
    assert len(runner.calls) == 5 and not any("claude-code--full" in c for c in runner.calls)
    assert outcome.night_skipped == [f"{LLAMA}--claude-code--full--000"] and outcome.exit_code == 0
    skip = [line for line in lines if line.startswith("  skip ")]
    assert len(skip) == 1 and f"{STRATA}--claude-code--full--000" in skip[0]
    assert "already started this night" in skip[0]


def test_one_batch_starts_one_full_level_claude_code_run_per_night(config, dirs):
    matrix = batch.load_matrix("first-round", config)
    plan = batch.make_plan(matrix, config, runs_dir=dirs[0], work_root=dirs[1])
    runner = FakeRunner(dirs[0], lambda: EVENING)
    outcome, _ = go(plan, config, dirs, runner=runner, probe_fn=fake_probe())
    assert [c for c in runner.calls if "claude-code--full" in c] == [f"{LLAMA}--claude-code--full--000"]
    assert len(outcome.night_skipped) == 9 and len(runner.calls) == 51


def test_a_run_from_the_previous_night_does_not_block(config, dirs):
    write_record(dirs[0], make_record(LLAMA, "claude-code", "full", 0, started=EVENING - timedelta(hours=10)))
    plan = batch.make_plan(batch.load_matrix("pilot", config), config, runs_dir=dirs[0], work_root=dirs[1])
    runner = FakeRunner(dirs[0], lambda: EVENING)
    outcome, _ = go(plan, config, dirs, runner=runner, probe_fn=fake_probe())
    assert plan.to_start == 5 and outcome.night_skipped == []


def test_a_batch_that_runs_into_a_new_night_starts_another(config, dirs):
    clock = iter(EVENING + timedelta(hours=h) for h in range(0, 1000))
    plan = batch.make_plan(batch.load_matrix("first-round", config), config, runs_dir=dirs[0], work_root=dirs[1])
    runner = FakeRunner(dirs[0], lambda: next(clock))
    go(plan, config, dirs, runner=runner, probe_fn=fake_probe(), clock=lambda: next(clock))
    assert len([c for c in runner.calls if "claude-code--full" in c]) >= 2


# -- probes -----------------------------------------------------------------------------


def test_a_probe_mismatch_refuses_that_harness_in_the_second_block(config, dirs):
    probe_fn = fake_probe({(STRATA, "claude-code"): "low"})
    plan = batch.make_plan(batch.load_matrix("first-round", config), config, runs_dir=dirs[0], work_root=dirs[1])
    runner = FakeRunner(dirs[0], lambda: EVENING)
    outcome, lines = go(plan, config, dirs, runner=runner, probe_fn=probe_fn)
    assert outcome.exit_code == 1 and len(outcome.refused) == 15
    assert all(f"{STRATA}--claude-code" in name for name in outcome.refused)
    assert any(f"{STRATA}--pi--" in c for c in runner.calls)
    assert not any(f"{STRATA}--claude-code" in c for c in runner.calls)
    assert any(line.startswith("  refused: claude-code: refusing to compare") for line in lines)
    assert probe_fn.calls == [(LLAMA, "pi"), (LLAMA, "claude-code"), (STRATA, "pi"), (STRATA, "claude-code")]
    stored = batch.load_probe(dirs[0] / batch.PROBES_DIR_NAME, STRATA, "claude-code")
    assert stored is not None and stored.received.effort == "low"


def test_a_resumed_second_block_compares_with_the_stored_probe(config, dirs):
    matrix = batch.load_matrix("first-round", config)
    probes = dirs[0] / batch.PROBES_DIR_NAME
    batch.save_probe(probes, fake_probe({(LLAMA, "pi"): "medium"})(LLAMA, "pi"), EVENING)
    for harness in matrix.harnesses:
        for level in matrix.levels:
            for i in range(matrix.runs):
                write_record(dirs[0], make_record(LLAMA, harness, level, i, started=EVENING - timedelta(days=3)))
    plan = batch.make_plan(matrix, config, runs_dir=dirs[0], work_root=dirs[1])
    assert len(plan.blocks[0].runs) == 0
    probe_fn = fake_probe()
    outcome, _ = go(plan, config, dirs, runner=FakeRunner(dirs[0], lambda: EVENING), probe_fn=probe_fn)
    assert probe_fn.calls == [(STRATA, "pi"), (STRATA, "claude-code")]
    assert len(outcome.refused) == 15 and all("--pi--" in n for n in outcome.refused)


def test_dry_run_runs_no_probe(config, dirs):
    plan = batch.make_plan(batch.load_matrix("pilot", config), config, runs_dir=dirs[0], work_root=dirs[1])
    lines = batch.describe_plan(plan, runs_dir=dirs[0], now=EVENING)
    assert [line for line in lines if line.startswith("  probe ")] == [
        f"  probe pi on {LLAMA}", f"  probe claude-code on {LLAMA}"]
    assert not (dirs[0] / batch.PROBES_DIR_NAME).exists()


# -- records --check ----------------------------------------------------------------------


def check(argv: list[str], dirs, capsys) -> tuple[int, str]:
    code = cmd_records.main(["--check", "--runs-dir", str(dirs[0]), *argv])
    return code, capsys.readouterr().out


def test_check_counts_each_cell_and_fails_when_one_is_short(dirs, capsys):
    for harness, level in [("pi", "quick"), ("pi", "fast"), ("pi", "full"),
                           ("claude-code", "quick"), ("claude-code", "fast")]:
        write_record(dirs[0], make_record(LLAMA, harness, level, 0))
    code, out = check(["--matrix", "pilot"], dirs, capsys)
    assert code == 1
    assert f"{LLAMA} / pi / quick: 1/1 valid  ok" in out
    assert f"{LLAMA} / claude-code / full: 0/1 valid  SHORT" in out
    assert "check: FAIL - 1 cell(s) short, 0 invalid record(s)" in out
    write_record(dirs[0], make_record(LLAMA, "claude-code", "full", 0))
    code, out = check(["--matrix", "pilot"], dirs, capsys)
    assert code == 0 and out.rstrip().endswith("check: ok")


def test_check_fails_on_an_invalid_record_even_when_the_cell_is_full(dirs, capsys):
    for harness in ("pi", "claude-code"):
        for level in ("quick", "fast", "full"):
            write_record(dirs[0], make_record(LLAMA, harness, level, 1))
    write_record(dirs[0], make_record(LLAMA, "claude-code", "quick", 0, valid=False))
    (dirs[0] / f"{LLAMA}--pi--full--002").mkdir()
    code, out = check(["--matrix", "pilot"], dirs, capsys)
    assert code == 1
    assert f"{LLAMA} / claude-code / quick: 1/1 valid, 1 invalid  INVALID" in out
    assert f"invalid {LLAMA}--claude-code--quick--000: invalid harness-exit" in out
    assert f"no record: {LLAMA}--pi--full--002" in out
    assert "check: FAIL - 0 cell(s) short, 1 invalid record(s)" in out


def test_check_treats_schema_failures_and_mismatched_records_as_invalid(dirs, capsys):
    path = write_record(dirs[0], make_record(LLAMA, "pi", "quick", 0))
    rec = json.loads(path.read_text())
    del rec["score"]
    path.write_text(json.dumps(rec))
    write_record(dirs[0], make_record(LLAMA, "pi", "fast", 0))
    moved = dirs[0] / f"{LLAMA}--pi--fast--003"
    (dirs[0] / f"{LLAMA}--pi--fast--000").rename(moved)
    code, out = check([], dirs, capsys)
    assert code == 1
    assert "schema: score: missing" in out
    assert "names another arm, level or index than its run dir" in out
    assert "2 run dir(s), 2 invalid record(s)" in out


def test_check_without_records_dir_and_the_listing(dirs, capsys):
    code, out = check(["--matrix", "first-round"], dirs, capsys)
    assert code == 1 and "check: FAIL - 12 cell(s) short" in out
    write_record(dirs[0], make_record(LLAMA, "pi", "quick", 0))
    (dirs[0] / batch.PROBES_DIR_NAME).mkdir()
    assert cmd_records.main(["--runs-dir", str(dirs[0])]) == 0
    listing = capsys.readouterr().out.splitlines()
    assert listing == [f"{LLAMA}--pi--quick--000: valid complete, score 0.500, started {EVENING.isoformat()}"]
