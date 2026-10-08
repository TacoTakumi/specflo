"""One bench run end to end: refusals, the assembled run record, engine versions and telemetry.

No test here needs a model, pi, claude, herdr or llama-swap: the rig is a
stand-in that answers preflight and /running, and the harness is a small python
script that writes a pi session log and an edit to the workdir, then exits. The
real specflo seeds the workdir and the real grader grades the final tree.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from modelbench import arms, cc_continue, cmd_run, launch_cc, lifecycle, normlog, preflight, record, run, telemetry

BENCH = Path(__file__).resolve().parents[2] / "bench"
SAMPLES = Path(__file__).resolve().parent / "samples"
ENTRY = "swift15-flash-next-iq4xs-mtp-vision"

LLAMA_CMD = (
    "/opt/engines/builds/llamacpp-test/build/bin/llama-server --port 10044 --jinja\n"
    "--model /models/swift/Swift-IQ4_XS-00001-of-00003.gguf\n"
    "-md /models/mtp.gguf --ctx-size 262144\n"
    "--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0\n"
)

# Writes one pi session (the sample, its model ids set to the entry) where pi
# would, makes an edit in the workdir, then exits by itself.
HARNESS = r'''
import json, os, pathlib, sys
sample, entry, agent_dir = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
sessions = agent_dir / "sessions" / "--work--"
sessions.mkdir(parents=True, exist_ok=True)
text = pathlib.Path(sample).read_text().replace("bench-model-a", entry)
(sessions / "2026-10-08T10-00-00_s1.jsonl").write_text(text)
(pathlib.Path.cwd() / "NOTES.txt").write_text("the agent was here\n")
print(json.dumps({"type": "agent_settled"}), flush=True)
'''


class FakeRig:
    """llama-swap as a stand-in: what is loaded, and each entry's command line."""

    def __init__(self, loaded: list[str] | None = None, bench_loaded: set[str] | None = None,
                 cmd: str = LLAMA_CMD) -> None:
        self.loaded = list(loaded if loaded is not None else [ENTRY])
        self.bench_loaded = set(bench_loaded or ())
        self.cmd = cmd
        self.calls: list[tuple[str, str]] = []

    def preflight(self, run_: arms.Run) -> list[str]:
        self.calls.append(("preflight", run_.entry))
        foreign = preflight.foreign_models(self.loaded, run_.entry, self.bench_loaded)
        if foreign:
            raise preflight.PreflightError(f"foreign: {', '.join(foreign)}", foreign)
        return list(self.loaded)

    def record_load(self, entry: str) -> None:
        self.calls.append(("record_load", entry))
        self.bench_loaded.add(entry)

    def load(self, entry: str) -> None:
        self.calls.append(("load", entry))
        self.loaded = [entry]

    def running(self) -> list[dict]:
        return [{"model": m, "cmd": self.cmd} for m in self.loaded]


def _builder(script: Path):
    def build(arm, *, workdir, run_dir, specflo, env, limits, settings):
        agent_dir = Path(run_dir) / "pi-agent"
        return lifecycle.HarnessSpec(
            harness="pi", argv=[sys.executable, str(script), str(SAMPLES / "pi-session.jsonl"), arm.entry,
                                str(agent_dir)],
            env=env, cwd=workdir,
            report=lambda: {"harness": "pi 9.9.9", "config_hash": "sha256:test", "agent_dir": str(agent_dir)},
        )
    return build


def _stamped_log(path: Path, stamp: str) -> Path:
    path.write_text(
        f"{stamp} 0.12.004.230 I slot launch_slot_: id  0 | task 39 | processing task\n"
        f"{stamp} 0.12.400.000 I slot print_timing: id  0 | task 39 | prompt eval time =     500.00 ms /  1000 tokens\n"
        f"{stamp} 0.12.900.000 I slot print_timing: id  0 | task 39 |        eval time =     250.00 ms /    50 tokens\n"
        f"{stamp} 0.12.900.100 I slot      release: id  0 | task 39 | stop processing: n_tokens = 1049, truncated = 0\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def synthetic(tmp_path: Path):
    """run_one's arguments for a stand-in pi run in tmp_path."""
    script = tmp_path / "harness.py"
    script.write_text(HARNESS)
    return {
        "entry": ENTRY, "harness": "pi", "level": "quick", "run_index": 0,
        "rig": FakeRig(), "runs_dir": tmp_path / "runs", "work_root": tmp_path / "work",
        "llama_swap_log": tmp_path / "llama-swap.log", "headless": True,
        "wall_clock": 60.0, "stall": 30.0,
        "builders": {"pi": _builder(script)}, "poll": 0.05, "stop_grace": 1.0,
    }


# -- refusals ---------------------------------------------------------------------------


@pytest.mark.parametrize(("field", "change"), [
    ("entry", {"entry": None}),
    ("entry", {"entry": "no-such-entry"}),
    ("harness", {"harness": None}),
    ("harness", {"harness": "codex"}),
    ("level", {"level": None}),
    ("level", {"level": "medium"}),
    ("run_index", {"run_index": None}),
    ("run_index", {"run_index": -1}),
    ("run_index", {"run_index": "0"}),
])
def test_a_run_with_a_missing_or_unknown_field_is_refused_before_the_rig(synthetic, field, change):
    rig = synthetic["rig"]
    with pytest.raises(arms.ArmError) as exc:
        run.run_one(**{**synthetic, **change})
    assert exc.value.field == field
    assert rig.calls == []
    assert not synthetic["runs_dir"].exists() and not synthetic["work_root"].exists()


@pytest.mark.parametrize(("argv", "field"), [
    (["--harness", "pi", "--level", "quick"], "entry"),
    (["--entry", "nope", "--harness", "pi", "--level", "quick"], "entry"),
    (["--entry", ENTRY, "--level", "quick"], "harness"),
    (["--entry", ENTRY, "--harness", "codex", "--level", "quick"], "harness"),
    (["--entry", ENTRY, "--harness", "pi"], "level"),
    (["--entry", ENTRY, "--harness", "pi", "--level", "medium"], "level"),
])
def test_the_command_refuses_a_missing_or_unknown_field(tmp_path, capsys, argv, field):
    code = cmd_run.main([*argv, "--runs-dir", str(tmp_path / "runs"), "--work-root", str(tmp_path / "w"),
                         "--state", str(tmp_path / "state.json"), "--base-url", "http://127.0.0.1:9"])
    assert code == 2
    assert f"{field}:" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["-1", "x", "1.5"])
def test_the_command_refuses_a_bad_run_index(tmp_path, capsys, bad):
    with pytest.raises(SystemExit) as exc:
        cmd_run.main(["--entry", ENTRY, "--harness", "pi", "--level", "quick", "--run-index", bad])
    assert exc.value.code == 2
    assert "--run-index" in capsys.readouterr().err


def test_the_next_run_index_skips_runs_already_made(tmp_path):
    config = arms.load_config()
    runs, work = tmp_path / "runs", tmp_path / "work"
    args = {"entry": ENTRY, "harness": "pi", "level": "quick", "runs_dir": runs, "work_root": work}
    assert run.next_run_index(config, **args) == 0
    (runs / f"{ENTRY}--pi--quick--000").mkdir(parents=True)
    (work / f"{ENTRY}--pi--quick--001").mkdir(parents=True)
    assert run.next_run_index(config, **args) == 2
    assert run.next_run_index(config, **{**args, "harness": "claude-code"}) == 0
    with pytest.raises(arms.ArmError, match="level"):
        run.next_run_index(config, **{**args, "level": "medium"})


def test_a_foreign_model_refuses_the_run_and_nothing_is_made(synthetic):
    synthetic["rig"] = FakeRig(loaded=["someone-elses-model"])
    with pytest.raises(preflight.PreflightError, match="someone-elses-model"):
        run.run_one(**synthetic)
    assert not synthetic["runs_dir"].exists() and not synthetic["work_root"].exists()


def test_an_arm_level_and_index_that_already_ran_is_refused(synthetic):
    (synthetic["runs_dir"] / f"{ENTRY}--pi--quick--000").mkdir(parents=True)
    with pytest.raises(run.RunError, match="already ran"):
        run.run_one(**synthetic)


def test_the_run_passes_its_own_run_dir_to_the_contamination_check(synthetic, monkeypatch):
    seen = {}
    real = run.diagnostics.diagnose

    def spy(log, **kw):
        seen.update(kw)
        return real(log, **kw)

    monkeypatch.setattr(run.diagnostics, "diagnose", spy)
    path, _ = run.run_one(**synthetic)
    assert seen["own_dirs"] == [str(path.parent)]


def test_the_run_protects_sibling_workdirs_and_run_dirs_but_not_its_own(synthetic, monkeypatch):
    seen = {}
    real = run.diagnostics.diagnose

    def spy(log, **kw):
        seen.update(kw)
        return real(log, **kw)

    monkeypatch.setattr(run.diagnostics, "diagnose", spy)
    path, _ = run.run_one(**synthetic)
    work_root, runs_dir = synthetic["work_root"].resolve(), synthetic["runs_dir"].resolve()
    assert {str(work_root), str(runs_dir)} <= set(seen["protected"])
    assert Path(seen["workdir"]).parent == work_root and seen["own_dirs"] == [str(path.parent)]
    def reads(path: str) -> dict:
        call = {"name": "read", "id": "c1", "time": 1.0, "is_error": False, "arguments": {"path": path}}
        return real({"requests": [], "tool_calls": [call]}, **seen)

    assert reads(f"{work_root}/other--pi--quick--000/tinytodo/store.py")["contaminated"] is True
    assert reads(f"{runs_dir}/other--pi--quick--000/record.json")["contaminated"] is True
    assert reads(f"{seen['workdir']}/tinytodo/store.py")["contaminated"] is False
    assert reads(f"{path.parent}/harness.out")["contaminated"] is False


def test_claude_code_sessions_include_the_pass_in_flight_when_the_run_was_killed(tmp_path):
    run_dir = tmp_path / "run"
    project = run_dir / launch_cc.CONFIG_DIR_NAME / "projects" / "-tmp-work"
    (project / "s1" / "subagents").mkdir(parents=True)
    first, second = project / "s1.jsonl", project / "s2.jsonl"
    for i, path in enumerate((first, second, project / "s1" / "subagents" / "agent-a.jsonl")):
        path.write_text("{}\n")
        os.utime(path, (1000 + i, 1000 + i))
    (run_dir / cc_continue.RECORD_FILE).write_text(json.dumps({"passes": [{"session_file": str(first)}]}))
    assert run.session_files("claude-code", run_dir) == [first, second]


def test_an_entry_the_bench_load_evicted_is_foreign_when_the_operator_loads_it(tmp_path):
    from .test_preflight import StubSwap

    config = arms.load_config()
    a, b = "swift15-flash-next-iq4xs-mtp-vision", "swift15-flash-next-iq4xs-strata-2x3090"
    state = tmp_path / "loaded.json"
    for entry in (a, b):
        preflight.record_bench_load(state, entry)
    with StubSwap([b]) as stub:  # the bench's load of b evicted a
        assert [m["model"] for m in run.Rig(stub.url, state).running()] == [b]
    assert preflight.read_bench_loaded(state) == {b}
    with StubSwap([a]) as stub:  # the operator loads a himself
        with pytest.raises(preflight.PreflightError) as err:
            run.Rig(stub.url, state).preflight(arms.Run(b, "pi", "quick", 0))
    assert err.value.foreign == [a]


# -- the record -------------------------------------------------------------------------


def _leaf_paths(fields: dict, prefix: str = "") -> list[str]:
    out = []
    for name, spec in fields.items():
        out.append(f"{prefix}{name}")
        if isinstance(spec, dict):
            out.extend(_leaf_paths(spec, f"{prefix}{name}."))
    return out


def _get(rec: dict, path: str):
    node = rec
    for part in path.split("."):
        node = node[part]
    return node


def test_a_synthetic_run_writes_a_record_that_passes_the_schema(synthetic):
    path, rec = run.run_one(**synthetic)
    assert record.validate_record(rec) == []
    assert json.loads(path.read_text()) == rec
    for field in _leaf_paths(record.REQUIRED_FIELDS):
        _get(rec, field)  # every required field is there
    name = f"{ENTRY}--pi--quick--000"
    assert path == (synthetic["runs_dir"] / name / run.RECORD_FILE).resolve()
    assert rec["arm"] == {"entry": ENTRY, "harness": "pi"}
    assert (rec["level"], rec["run_index"]) == ("quick", 0)
    assert rec["versions"]["harness"] == "pi 9.9.9"
    assert rec["versions"]["engine_build"].startswith("llamacpp-test")
    assert rec["versions"]["gguf_path"] == "/models/swift/Swift-IQ4_XS-00001-of-00003.gguf"
    assert rec["versions"]["entry"] == ENTRY
    assert rec["settings"]["sampling"] == {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0}
    assert rec["settings"]["context_window"] == 262144
    assert (rec["settings"]["autonomy"], rec["settings"]["pass_cap"]) == (run.AUTONOMY, run.PASS_CAP)
    assert rec["end_reason"] == lifecycle.END_HARNESS_EXIT
    assert rec["lifecycle"]["placement"] == "headless" and rec["lifecycle"]["pane_id"] is None
    assert (rec["lifecycle"]["wall_clock_cap"], rec["lifecycle"]["stall_limit"]) == (60.0, 30.0)
    # The session's model ids are the entry, no protected path was named, the tree was graded.
    assert rec["valid"] is True, rec["validity"]
    assert rec["validity"]["model_ids"] == [ENTRY]
    assert 0.0 <= rec["score"] <= 1.0 and rec["grade"]["total"] > 0
    assert rec["metrics"]["turns"] > 0
    assert rec["diagnostics"]["contaminated"] is False
    assert rec["passes"] == 1
    # Raw logs: every path the record names exists.
    logs = rec["logs"]
    for key in ("run_dir", "workdir", "harness_stdout", "harness_stderr", "lifecycle", "normalised"):
        assert Path(logs[key]).exists(), key
    assert [Path(p).name for p in logs["sessions"]] == ["2026-10-08T10-00-00_s1.jsonl"]
    normlog.load(logs["normalised"])
    # The workdir carried the auto settings in its one commit and holds the agent's edit.
    assert (Path(logs["workdir"]) / "NOTES.txt").exists()
    config = (Path(logs["workdir"]) / ".specflo" / "config.yaml").read_text()
    assert "autonomy: autonomous" in config and f"auto_max_passes: {run.PASS_CAP}" in config


def test_with_no_log_the_record_carries_the_telemetry_unavailable_marker(synthetic):
    _, rec = run.run_one(**synthetic)
    assert rec["telemetry"]["available"] is False and "not found" in rec["telemetry"]["reason"]
    assert record.validate_record(rec) == []


def test_a_log_with_no_stamps_in_the_window_gives_the_unavailable_marker(synthetic):
    _stamped_log(synthetic["llama_swap_log"], "2020-01-01T00:00:00.000-03:00")
    _, rec = run.run_one(**synthetic)
    assert rec["telemetry"] == telemetry.unavailable("no server timing line is stamped inside the run window")


def test_an_unstamped_log_gives_the_unavailable_marker(tmp_path):
    log = tmp_path / "plain.log"
    shutil.copyfile(SAMPLES / "llama-swap-plain.log", log)
    got = run.run_telemetry(log, "2026-10-07T20:00:00-03:00", "2026-10-07T22:00:00-03:00")
    assert got["available"] is False and "wall-clock column" in got["reason"]


def test_stamped_lines_in_the_window_are_counted(tmp_path):
    log = _stamped_log(tmp_path / "ls.log", "2026-10-08T08:13:58.208475-0300")
    got = run.run_telemetry(log, "2026-10-08T08:00:00-03:00", "2026-10-08T09:00:00-03:00")
    assert got["available"] is True
    assert got["prefill"]["tokens"] == 1000 and got["decode"]["tokens"] == 50


def test_the_entry_is_loaded_and_recorded_when_it_is_not_loaded(synthetic):
    synthetic["rig"] = rig = FakeRig(loaded=[])
    _, rec = run.run_one(**synthetic)
    assert ("record_load", ENTRY) in rig.calls and ("load", ENTRY) in rig.calls
    assert rec["running_after"] == [ENTRY]


def test_a_record_that_cannot_be_graded_is_invalid_with_score_zero(synthetic):
    def broken(*a, **kw):
        raise RuntimeError("no archive")

    _, rec = run.run_one(**synthetic, grade=broken)
    assert rec["score"] == 0.0 and rec["valid"] is False
    assert any("could not be graded" in r for r in rec["validity"]["reasons"])
    assert record.validate_record(rec) == []


# -- parts ------------------------------------------------------------------------------


def test_engine_info_reads_a_llama_cpp_command_line():
    info = run.engine_info({"model": ENTRY, "cmd": LLAMA_CMD}, "llama.cpp", commit=lambda b: "abc123")
    assert info["engine_build"] == "llamacpp-test@abc123"
    assert info["binary"] == "/opt/engines/builds/llamacpp-test/build/bin/llama-server"
    assert info["gguf_path"] == "/models/swift/Swift-IQ4_XS-00001-of-00003.gguf"
    assert info["ctx_size"] == 262144


def test_engine_info_reads_a_strata_config(tmp_path):
    conf = tmp_path / "strata.json"
    conf.write_text(json.dumps({
        "exe": "/opt/strata-v1/build/strata",
        "args": ["--pack", "/p", "--native", "/models/x.gguf", "--max-context", "131072"],
        "sampling": {"temperature": 0.7},
    }))
    cmd = f"/bin/sh -c 'cd /opt/strata-v1 && exec python -m serve.server --config {conf} --port ${{PORT}}'\n"
    info = run.engine_info({"model": "s", "cmd": cmd}, "strata", commit=lambda b: None)
    assert info["engine_build"] == "strata-v1"
    assert info["gguf_path"] == "/models/x.gguf"
    assert info["sampling"] == {"temperature": 0.7} and info["ctx_size"] == 131072


def test_engine_info_refuses_an_entry_without_a_command_line():
    with pytest.raises(run.RunError):
        run.engine_info({"model": ENTRY}, "llama.cpp")


def test_several_sessions_merge_into_one_log_with_unique_ids(tmp_path):
    one = tmp_path / "a.jsonl"
    two = tmp_path / "b.jsonl"
    shutil.copyfile(SAMPLES / "pi-session.jsonl", one)
    shutil.copyfile(SAMPLES / "pi-session.jsonl", two)
    log, errors = run.normalised_log("pi", [one, two])
    assert errors == []
    single, _ = run.normalised_log("pi", [one])
    assert len(log["requests"]) == 2 * len(single["requests"])
    assert {r["session"] for r in log["requests"]} == {1, 2}
    normlog.check(log)


def test_an_unreadable_session_is_kept_as_an_error(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n")
    log, errors = run.normalised_log("pi", [bad])
    assert log["requests"] == [] and len(errors) == 1


def test_limits_come_from_arguments_then_the_arm_config_then_the_provisional_defaults():
    config = arms.Config(entries={}, harnesses=arms.HARNESSES,
                         levels={"quick": {"stall_limit": 120}, "fast": {}, "full": {}})
    limits, source = run.resolve_limits("quick", config)
    assert limits == lifecycle.Limits(run.PROVISIONAL_LIMITS["quick"][0], 120.0)
    assert source == {"wall_clock": "provisional default", "stall_limit": "arm config"}
    limits, source = run.resolve_limits("quick", config, wall_clock=10, stall=5)
    assert limits == lifecycle.Limits(10.0, 5.0) and set(source.values()) == {"argument"}


def test_an_engine_limit_factor_scales_the_config_and_provisional_limits_but_not_arguments():
    config = arms.Config(entries={"slow": {"engine": "llama.cpp"}, "fast": {"engine": "strata"}},
                         harnesses=arms.HARNESSES,
                         levels={"quick": {"stall_limit": 120}, "fast": {}, "full": {}},
                         engines={"llama.cpp": {"limit_factor": 2.5}})
    limits, source = run.resolve_limits("quick", config, entry="slow")
    assert limits == lifecycle.Limits(run.PROVISIONAL_LIMITS["quick"][0] * 2.5, 300.0)
    assert source == {"wall_clock": "provisional default x2.5", "stall_limit": "arm config x2.5"}
    limits, source = run.resolve_limits("quick", config, entry="slow", wall_clock=10)
    assert limits.wall_clock == 10.0 and source["wall_clock"] == "argument"
    limits, source = run.resolve_limits("quick", config, entry="fast")
    assert limits == lifecycle.Limits(run.PROVISIONAL_LIMITS["quick"][0], 120.0)
    assert source == {"wall_clock": "provisional default", "stall_limit": "arm config"}

