"""Claude Code continuation: one fresh `claude -p` per specflo pass, the bench as the outer loop.

The loop tests use a stand-in `claude` (a script that plays a list of steps per
pass and writes a session file) and a stand-in `specflo` (a script over a JSON
state file that answers `auto --json`, `status --json`, `advance` and `task
done`), so they need no model and no real project. One test runs the real
claude binary against a stub Anthropic endpoint. The rig test runs the real
claude and the real specflo on a sealed fast-level workdir against the arm's
llama-swap entry and is run only with `-m rig`.
"""

from __future__ import annotations

import json
import os
import sys
import time
import warnings
from pathlib import Path

import pytest

from modelbench import cc_continue as cc
from modelbench import launch_cc, norm_cc

from .test_launch_cc import ENTRY, HAVE_CLAUDE, AnthropicStub

PHASES = ["brainstorm", "spec", "plan", "execute"]

FAKE_SPECFLO = r'''
import json, os, sys
state_path = os.environ["FAKE_SPECFLO_STATE"]
with open(state_path) as f:
    state = json.load(f)
args = sys.argv[1:]
with open(state_path + ".calls", "a") as f:
    f.write(" ".join(args) + "\n")
def save():
    with open(state_path, "w") as f:
        json.dump(state, f)
if args[:2] == ["auto", "--json"]:
    if state["auto"]:
        item = state["auto"].pop(0)
        save()
        if item == "fail":
            sys.exit(2)
        print(json.dumps(item))
    else:
        print(json.dumps({"payload": "The project is complete.", "stop": True, "reason": "project-complete"}))
elif args[:2] == ["status", "--json"]:
    print(json.dumps({
        "phase": state["phases"][state["phase"]],
        "progress": {"done": state["done"]},
        "context_threshold_percent": state["threshold"],
        "auto_run": {"under_way": state["under_way"]},
    }))
elif args[:1] == ["advance"]:
    state["phase"] += 1
    save()
    print("Advanced. You may clear context now.")
elif args[:2] == ["task", "done"]:
    state["done"] += 1
    save()
    print("Task done. You may clear context now.")
'''

FAKE_CLAUDE = r'''
import json, os, pathlib, subprocess, sys, time
argv = sys.argv[1:]
sid = argv[argv.index("--session-id") + 1]
prompt = sys.stdin.read()
plan_path = os.environ["FAKE_CLAUDE_PLAN"]
counter = pathlib.Path(plan_path + ".count")
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
with open(plan_path) as f:
    plan = json.load(f)
steps = plan[min(n, len(plan) - 1)]
with open(plan_path + ".calls", "a") as f:
    f.write(json.dumps({"session_id": sid, "prompt": prompt, "argv": argv,
                        "config_dir": os.environ.get("CLAUDE_CONFIG_DIR")}) + "\n")
session = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "-work" / (sid + ".jsonl")
session.parent.mkdir(parents=True, exist_ok=True)
def emit(event):
    print(json.dumps(event), flush=True)
    with session.open("a") as f:
        f.write(json.dumps(event) + "\n")
emit({"type": "system", "subtype": "init", "session_id": sid})
final = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
for i, step in enumerate(steps):
    if "crash" in step:
        sys.exit(step["crash"])
    if "hang" in step:
        time.sleep(3600)
    if "error" in step:
        final = {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": step["error"]}
    if "bash" in step:
        emit({"type": "assistant", "parent_tool_use_id": None, "message": {
            "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": step["bash"]}}],
            "usage": {"input_tokens": step.get("tokens", 10), "cache_read_input_tokens": 0}}})
        out = subprocess.run(step["bash"], shell=True, capture_output=True, text=True).stdout
        emit({"type": "user", "parent_tool_use_id": None, "message": {
            "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": out}]}})
emit(final)
'''


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return path


def _cont(payload: str) -> dict:
    return {"payload": payload, "stop": False, "reason": None}


class Rig:
    """A stand-in claude and specflo in tmp_path, and a launch that runs them."""

    def __init__(self, tmp_path: Path, auto: list, plan: list[list[dict]], threshold: int = 25,
                 under_way: bool = True) -> None:
        self.tmp = tmp_path
        self.bindir = tmp_path / "fakebin"
        self.specflo = str(_script(self.bindir / "specflo", FAKE_SPECFLO))
        self.claude = str(_script(tmp_path / "claude-bin" / "claude", FAKE_CLAUDE))
        self.state = tmp_path / "specflo-state.json"
        self.state.write_text(json.dumps({"phases": PHASES, "phase": 0, "done": 0, "threshold": threshold,
                                          "under_way": under_way, "auto": auto}))
        self.plan = tmp_path / "claude-plan.json"
        self.plan.write_text(json.dumps(plan))
        self.workdir = tmp_path / "work"
        self.workdir.mkdir()
        self.config = tmp_path / "run" / launch_cc.CONFIG_DIR_NAME
        self.config.mkdir(parents=True)
        self.launch = launch_cc.CcLaunch(
            argv=[self.claude, "--model", ENTRY],
            env={"PATH": f"{self.bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
                 "CLAUDE_CONFIG_DIR": str(self.config),
                 "FAKE_SPECFLO_STATE": str(self.state), "FAKE_CLAUDE_PLAN": str(self.plan)},
            cwd=self.workdir, config_dir=self.config, claude_version="test", config_hash="sha256:test",
        )

    def run(self, **kw) -> cc.RunResult:
        args = {"run_dir": self.tmp / "run", "specflo": self.specflo, "window": 1000,
                "max_passes": 5, "wall_clock": 60.0, **kw}
        return cc.run_passes(self.launch, **args)

    def claude_calls(self) -> list[dict]:
        path = Path(f"{self.plan}.calls")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def specflo_calls(self) -> list[str]:
        return Path(f"{self.state}.calls").read_text().splitlines()


# -- the pass loop ----------------------------------------------------------------


def test_a_stop_before_any_pass_ends_the_run_with_specflos_reason(tmp_path: Path):
    rig = Rig(tmp_path, auto=[{"payload": "AUTO-RUN ESCALATION: stalled.", "stop": True, "reason": "stall"}],
              plan=[[]])
    result = rig.run()
    assert result.end_reason == "stall"
    assert result.end_detail == "AUTO-RUN ESCALATION: stalled."
    assert result.passes == []
    assert rig.claude_calls() == []


def test_each_pass_is_a_fresh_session_fed_the_payload_verbatim(tmp_path: Path):
    rig = Rig(tmp_path, auto=[_cont("payload one\nline 2"), _cont("payload two")], plan=[[]])
    result = rig.run()
    assert result.end_reason == "project-complete"
    assert result.end_detail == "The project is complete."
    calls = rig.claude_calls()
    assert [c["prompt"] for c in calls] == ["payload one\nline 2", "payload two"]
    assert len({c["session_id"] for c in calls}) == 2
    assert result.session_ids() == [c["session_id"] for c in calls]
    for call in calls:
        argv = call["argv"]
        assert argv[:2] == ["--model", ENTRY]
        assert argv[2:6] == list(cc.PASS_ARGS)
        assert "--resume" not in argv and "--continue" not in argv
        assert "--no-session-persistence" not in argv
        # One run copy of the config dir for every pass.
        assert call["config_dir"] == str(rig.config)
    for record in result.passes:
        assert record.ended_by == cc.ENDED_EXIT
        assert record.exit_code == 0
        assert record.result_subtype == "success"
        assert record.session_file == str(rig.config / "projects" / "-work" / f"{record.session_id}.jsonl")
        assert Path(record.stream_log).read_text().count("\n") == 2
    assert [c for c in rig.specflo_calls() if c.startswith("auto")] == ["auto --json"] * 3


def test_the_record_is_written_after_every_pass(tmp_path: Path):
    rig = Rig(tmp_path, auto=[_cont("one"), _cont("two")], plan=[[]])
    seen = []
    result = rig.run(on_pass=lambda r: seen.append(json.loads((tmp_path / "run" / cc.RECORD_FILE).read_text())))
    assert [s["context_passes"] for s in seen] == [1, 2]
    final = json.loads((tmp_path / "run" / cc.RECORD_FILE).read_text())
    assert final == result.to_dict()
    assert final["end_reason"] == "project-complete"
    assert [p["session_id"] for p in final["passes"]] == result.session_ids()


def test_an_armed_seam_ends_the_pass_and_the_next_pass_starts_fresh(tmp_path: Path):
    # 300 of 1000 tokens is 30 %, over the 25 % threshold specflo reports.
    plan = [[{"bash": "specflo advance", "tokens": 300}, {"hang": True}], []]
    rig = Rig(tmp_path, auto=[_cont("brainstorm pass"), _cont("spec pass")], plan=plan)
    started = time.monotonic()
    result = rig.run()
    assert time.monotonic() - started < 30
    assert result.end_reason == "project-complete"
    first, second = result.passes
    assert first.ended_by == cc.ENDED_SEAM
    assert first.seam == "the phase is now spec"
    assert (first.phase_before, first.phase_after) == ("brainstorm", "spec")
    assert first.context_tokens == 300
    assert not first.failed
    assert second.ended_by == cc.ENDED_EXIT
    assert second.phase_before == "spec"
    assert first.session_id != second.session_id


def test_a_task_reaching_done_is_a_seam(tmp_path: Path):
    plan = [[{"bash": "specflo task done", "tokens": 300}, {"hang": True}], []]
    rig = Rig(tmp_path, auto=[_cont("one")], plan=plan)
    result = rig.run()
    assert result.passes[0].seam == "a task reached done (1 done)"
    assert (result.passes[0].done_before, result.passes[0].done_after) == (0, 1)


def test_an_unarmed_seam_does_not_end_the_pass(tmp_path: Path):
    plan = [[{"bash": "specflo advance", "tokens": 100}, {"bash": "specflo advance", "tokens": 200}]]
    rig = Rig(tmp_path, auto=[_cont("one")], plan=plan)
    result = rig.run()
    (record,) = result.passes
    assert record.ended_by == cc.ENDED_EXIT and record.seam is None
    assert (record.phase_before, record.phase_after) == ("brainstorm", "plan")


def test_a_threshold_override_of_zero_arms_at_once(tmp_path: Path):
    plan = [[{"bash": "true", "tokens": 1}, {"bash": "specflo advance", "tokens": 1}, {"hang": True}], []]
    rig = Rig(tmp_path, auto=[_cont("one"), _cont("two")], plan=plan)
    result = rig.run(threshold_percent=0)
    assert result.passes[0].ended_by == cc.ENDED_SEAM
    assert len(result.passes) == 2


def test_a_seam_outside_an_auto_run_does_not_end_the_pass(tmp_path: Path):
    plan = [[{"bash": "specflo advance", "tokens": 900}]]
    rig = Rig(tmp_path, auto=[_cont("one")], plan=plan, under_way=False)
    result = rig.run()
    assert result.passes[0].ended_by == cc.ENDED_EXIT


def test_the_bench_pass_cap_ends_the_run(tmp_path: Path):
    rig = Rig(tmp_path, auto=[_cont(f"p{i}") for i in range(10)], plan=[[]])
    result = rig.run(max_passes=2)
    assert result.end_reason == cc.END_PASS_LIMIT
    assert len(result.passes) == 2
    # The third report was read (it is what said continue) but no third claude started.
    assert len(rig.claude_calls()) == 2


def test_a_crashed_pass_ends_the_run(tmp_path: Path):
    rig = Rig(tmp_path, auto=[_cont("one"), _cont("two")], plan=[[{"crash": 3}]])
    result = rig.run()
    assert result.end_reason == cc.END_PASS_FAILED
    (record,) = result.passes
    assert record.exit_code == 3 and record.failed
    assert record.result_subtype is None
    assert "pass 1 exited 3" in result.end_detail


def test_an_error_result_ends_the_run(tmp_path: Path):
    rig = Rig(tmp_path, auto=[_cont("one"), _cont("two")], plan=[[{"error": "API Error: 500"}]])
    result = rig.run()
    assert result.end_reason == cc.END_PASS_FAILED
    assert result.passes[0].result_text == "API Error: 500"
    assert result.passes[0].result_is_error is True


def test_the_wall_clock_ends_a_hung_pass(tmp_path: Path):
    rig = Rig(tmp_path, auto=[_cont("one"), _cont("two")], plan=[[{"hang": True}]])
    started = time.monotonic()
    result = rig.run(wall_clock=2.0)
    assert time.monotonic() - started < 20
    assert result.end_reason == cc.END_WALL_CLOCK
    assert result.passes[0].ended_by == cc.ENDED_TIMEOUT
    assert not result.passes[0].failed


def test_an_unreadable_auto_report_ends_the_run(tmp_path: Path):
    rig = Rig(tmp_path, auto=["fail"], plan=[[]])
    result = rig.run()
    assert result.end_reason == cc.END_AUTO_FAILED
    assert result.passes == []


# -- pieces -----------------------------------------------------------------------


def test_seam_rule_matches_the_pi_extension():
    last = cc.Snapshot(phase="plan", done=2)
    assert cc.describe_seam(last, cc.Snapshot(phase="plan", done=2)) is None
    assert cc.describe_seam(last, cc.Snapshot(phase="plan", done=1)) is None
    assert cc.describe_seam(last, cc.Snapshot(phase="plan", done=3)) == "a task reached done (3 done)"
    assert cc.describe_seam(last, cc.Snapshot(phase="execute", done=2)) == "the phase is now execute"
    assert cc.describe_seam(last, cc.Snapshot(phase=None, done=2)) == "the phase changed"


def test_arming_is_a_percent_of_the_window_and_an_unknown_threshold_never_arms():
    assert cc.armed(250, 1000, 25) and not cc.armed(249, 1000, 25)
    assert not cc.armed(10**9, 1000, None)
    assert cc.armed(0, 1000, 0)


def test_context_tokens_count_prompt_tokens_of_main_assistant_events_only():
    usage = {"input_tokens": 5, "cache_creation_input_tokens": 7, "cache_read_input_tokens": 11,
             "output_tokens": 99}
    assert cc.context_tokens({"type": "assistant", "message": {"usage": usage}}) == 23
    assert cc.context_tokens({"type": "assistant", "parent_tool_use_id": "t1",
                              "message": {"usage": usage}}) is None
    assert cc.context_tokens({"type": "user", "message": {"usage": usage}}) is None


def test_context_window_is_the_pi_arms_window():
    assert cc.context_window(ENTRY) == 262144


def test_specflo_bin_dir_holds_only_a_link_to_specflo(tmp_path: Path):
    target = _script(tmp_path / "venv" / "bin" / "specflo", "print('x')\n")
    (tmp_path / "venv" / "bin" / "python").write_text("")
    bindir = cc.specflo_bin_dir(tmp_path / "run", str(target))
    assert sorted(p.name for p in bindir.iterdir()) == ["specflo"]
    assert (bindir / "specflo").resolve() == target.resolve()
    assert cc.specflo_bin_dir(tmp_path / "run", str(target)) == bindir


def test_specflo_bin_dir_given_its_own_link_keeps_pointing_at_specflo(tmp_path: Path):
    target = _script(tmp_path / "venv" / "bin" / "specflo", "print('x')\n")
    bindir = cc.specflo_bin_dir(tmp_path / "run", str(target))
    cc.specflo_bin_dir(tmp_path / "run", str(bindir / "specflo"))
    assert (bindir / "specflo").resolve() == target.resolve()


# -- the real claude binary against a stub endpoint ----------------------------------


@pytest.mark.skipif(not HAVE_CLAUDE, reason="the real claude binary is not installed")
@pytest.mark.timeout(300)
def test_real_claude_runs_one_session_per_pass_under_one_config_copy(tmp_path: Path):
    rig = Rig(tmp_path, auto=[_cont("PASS-ONE payload"), _cont("PASS-TWO payload")], plan=[[]])
    # The first pass advances the phase and the seam ends it; the second just answers.
    stub = AnthropicStub([{"bash": "specflo advance"}, {"text": "done"}])
    try:
        with launch_cc.build_launch(
            ENTRY, workdir=rig.workdir, run_dir=tmp_path / "cc-run", base_url=stub.base_url,
            extra_env={"PATH": f"{rig.bindir}{os.pathsep}{os.environ['PATH']}",
                       "FAKE_SPECFLO_STATE": str(rig.state)},
        ) as launch:
            result = cc.run_passes(launch, run_dir=tmp_path / "cc-run", specflo=rig.specflo,
                                   window=1000, max_passes=4, wall_clock=240.0, threshold_percent=0)
    finally:
        stub.close()

    assert result.end_reason == "project-complete", result.to_dict()
    first, second = result.passes
    assert first.ended_by == cc.ENDED_SEAM and first.seam == "the phase is now spec"
    assert second.ended_by == cc.ENDED_EXIT and second.exit_code == 0
    assert first.session_id != second.session_id
    for record in result.passes:
        assert record.session_file and Path(record.session_file).parent.parent == launch.config_dir / "projects"
        assert norm_cc.normalise(record.session_file)["requests"]
    # Each pass's prompt reached the model as the first user message of its own conversation.
    firsts = [r["messages"] for r in stub.requests if r.get("tools")]
    prompts = {json.dumps(m[0]) for m in firsts}
    assert any("PASS-ONE payload" in p for p in prompts) and any("PASS-TWO payload" in p for p in prompts)
    assert not any("PASS-ONE payload" in json.dumps(m) and "PASS-TWO payload" in json.dumps(m) for m in firsts)
    # specflo's SessionStart hook ran once per fresh session.
    assert [c for c in rig.specflo_calls() if c.startswith("hook")] == ["hook reseed --format claude"] * 2


# -- rig: the arm's llama-swap entry on a sealed fast-level workdir ---------------


RIG_PASSES = 3
RIG_WALL_CLOCK = 3 * 3600.0


@pytest.mark.rig
@pytest.mark.timeout(int(RIG_WALL_CLOCK) + 900)
def test_rig_claude_crosses_a_phase_boundary_with_a_fresh_context(tmp_path_factory):
    """A real model through llama-swap passes a specflo phase boundary in a new session.

    Every seam ends the pass (threshold 0), so the first phase advance is a
    clear; the run stops after the first pass that starts in a later phase.
    """
    from modelbench import arms, preflight, workdir

    run = arms.Run(entry=ENTRY, harness="claude-code", level="fast", run_index=0)
    running = preflight.preflight(run)
    if ENTRY not in running:
        preflight.record_bench_load(preflight.DEFAULT_STATE, ENTRY)

    base = tmp_path_factory.mktemp("cc-continue")
    sealed = workdir.make_workdir(base / "work", "fast")
    run_dir = base / "run"
    start_phase = "brainstorm"

    def report(record: cc.PassRecord) -> None:
        print(f"pass {record.index}: session {record.session_id} ended by {record.ended_by}"
              f" ({record.seam}) exit {record.exit_code} phase {record.phase_before} -> {record.phase_after}"
              f" context {record.context_tokens}", flush=True)
        # Bound the rig run: stop once a pass has started in a later phase.
        if record.phase_before != start_phase:
            raise _Enough

    try:
        result, launch_report = cc.run(
            ENTRY, workdir=sealed.path, run_dir=run_dir, max_passes=RIG_PASSES,
            wall_clock=RIG_WALL_CLOCK, threshold_percent=0, on_pass=report,
        )
        passes = result.passes
    except _Enough:
        record = json.loads((run_dir / cc.RECORD_FILE).read_text())
        passes = [cc.PassRecord(**p) for p in record["passes"]]
    print(json.dumps([vars(p) for p in passes], indent=2), flush=True)

    assert len(passes) >= 2, passes
    first, *rest = passes
    assert first.phase_before == start_phase
    crossed = [p for p in rest if p.phase_before != start_phase]
    assert crossed, "no pass started past the brainstorm phase"
    assert len({p.session_id for p in passes}) == len(passes)
    for record in passes:
        assert record.session_file and Path(record.session_file).is_file()
        log = norm_cc.normalise(record.session_file)
        assert {r.get("model") for r in log["requests"]} == {ENTRY}
    # The later session's first user message is the auto payload, not an earlier conversation.
    later = [json.loads(line) for line in Path(crossed[0].session_file).read_text().splitlines()]
    users = [line for line in later if line.get("type") == "user"]
    assert "== specflo auto-mode bootstrap ==" in json.dumps(users[0])
    assert f"at the '{crossed[0].phase_before}' phase" in json.dumps(users[0])
    # A pass that ended by itself with nothing moved most likely stopped to ask
    # (specflo's SessionStart hook injects an ask-first directive into every
    # fresh session). Not a failure of the continuation; shown for the record.
    idle = [p.index for p in passes if p.ended_by == cc.ENDED_EXIT
            and (p.phase_after, p.done_after) == (p.phase_before, p.done_before)]
    if idle:
        warnings.warn(f"passes {idle} ended by themselves with no phase or task change", stacklevel=1)


class _Enough(Exception):
    """Raised from the rig test's pass callback to end the run once a boundary was crossed."""
