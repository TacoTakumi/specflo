"""The run lifecycle: end detection, cleanup of the harness tree, herdr placement and the recorded settings.

The harnesses here are small python scripts that act out one ending each, and
`specflo` is a stand-in over a JSON state file (status, project dir, config),
so no test needs a model, pi, claude or herdr. One test seeds a real sealed
workdir and runs the real specflo for the settings. The herdr tests use a
stand-in `herdr` binary behind the real adapter; the live herdr test runs only
with `-m rig`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from specflo.agent.herdr import HerdrAdapter

from modelbench import arms, cc_continue, launch_pi, record
from modelbench import lifecycle as lc
from modelbench import workdir as wd

ENTRY = "swift15-flash-next-iq4xs-mtp-vision"

STUB_SPECFLO = r'''
import json, os, sys
path = os.environ["STUB_SPECFLO_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
if args[:2] == ["status", "--json"]:
    print(json.dumps({"status": state["status"], "level": state["level"], "phase": "execute",
                      "dir": state["dir"], "auto_run": {"under_way": True}}))
elif args[:2] == ["config", "set"]:
    state.setdefault("config", {})[args[2]] = args[3]
    json.dump(state, open(path, "w"))
    print("Set.")
elif args[:2] == ["config", "get"]:
    print(state.get("config", {}).get(args[2], ""))
else:
    sys.exit(2)
'''

# One script, one ending per mode. It writes the pids of everything it starts
# to $HARNESS_PIDS, so a test can check that none is left.
HARNESS = r'''
import json, os, subprocess, sys, time
mode = sys.argv[1]
state_path = os.environ.get("STUB_SPECFLO_STATE")
pids = [os.getpid()]
def emit(event):
    print(json.dumps(event), flush=True)
def save_pids():
    with open(os.environ["HARNESS_PIDS"], "w") as f:
        json.dump(pids, f)
def set_status(value):
    state = json.load(open(state_path))
    state["status"] = value
    json.dump(state, open(state_path, "w"))
def run_state(data):
    state = json.load(open(state_path))
    with open(os.path.join(state["dir"], "auto-run.json"), "w") as f:
        json.dump(data, f)
def spawn_children():
    # A child, a child in its own session, and an orphan in its own session
    # (its parent shell exits at once): only the run token finds the last one.
    kid = subprocess.Popen(["sleep", "600"])
    own = subprocess.Popen(["sleep", "600"], start_new_session=True)
    orphan = subprocess.run(["sh", "-c", "setsid sleep 600 >/dev/null 2>&1 & echo $!"],
                            capture_output=True, text=True)
    pids.extend([kid.pid, own.pid, int(orphan.stdout)])
    save_pids()
def idle_forever():
    while True:
        time.sleep(1)

save_pids()
if mode in ("complete", "escalate", "kill-switch", "complete-busy"):
    line = sys.stdin.readline()
    with open(os.environ["HARNESS_STDIN"], "w") as f:
        f.write(line)
    emit({"type": "agent_start"})
    if mode == "complete":
        set_status("complete")
        emit({"type": "agent_settled"})
    elif mode == "complete-busy":
        set_status("complete")
        for i in range(8):
            emit({"type": "message_update", "i": i})
            time.sleep(0.1)
        with open(os.environ["HARNESS_SETTLED"], "w") as f:
            f.write(str(time.time()))
        emit({"type": "agent_settled"})
    elif mode == "escalate":
        run_state({"passes": 4, "ended": True})
        emit({"type": "agent_settled"})
        emit({"type": "extension_ui_request", "id": "n1", "method": "notify",
              "message": "AUTO-RUN ESCALATION: no forward progress.", "notifyType": "warning"})
    else:
        run_state({"passes": 2, "killed": True})
        emit({"type": "agent_settled"})
    idle_forever()
elif mode == "busy":
    spawn_children()
    while True:
        print("working", flush=True)
        time.sleep(0.05)
elif mode == "silent":
    spawn_children()
    print("one line, then nothing", flush=True)
    idle_forever()
elif mode == "exit":
    print("bye", flush=True)
    sys.exit(3)
elif mode == "env":
    with open(os.environ["HARNESS_ENV"], "w") as f:
        json.dump(dict(os.environ), f)
elif mode.startswith("cc-"):
    # A stand-in for the Claude Code loop child: write its result and exit.
    run_dir = os.getcwd()
    reason = mode[3:]
    if reason == "streams":
        # Silent on stdout; only the pass stream grows.
        with open(os.path.join(run_dir, "pass-01.stream.jsonl"), "a") as f:
            while True:
                f.write("{}\n")
                f.flush()
                time.sleep(0.05)
    if reason != "none":
        with open(os.path.join(run_dir, "cc-result.json"), "w") as f:
            json.dump({"result": {"end_reason": reason, "end_detail": "detail of " + reason},
                       "launch": {"harness": "claude-code test"}}, f)
    sys.exit(0 if reason != "none" else 1)
'''

FAKE_HERDR = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_HERDR_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
fail = os.environ.get("FAKE_HERDR_FAIL", "")
if fail and " ".join(args).startswith(fail):
    print("boom", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["workspace", "list"]:
    result = {"workspaces": [{"label": "other", "workspace_id": "w0"}]}
elif args[:2] == ["workspace", "create"]:
    result = {"workspace": {"workspace_id": "w1"}}
elif args[:2] == ["tab", "create"]:
    result = {"tab": {"tab_id": "w1:t1"}, "root_pane": {"pane_id": "w1:p7"}}
else:
    result = {}
print(json.dumps({"id": "x", "result": result}))
'''


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return path


def _alive(pids: list[int]) -> list[int]:
    return [p for p in pids if lc.alive(p)]


class Bench:
    """A stand-in specflo and harness in tmp_path, and the specs that run them."""

    def __init__(self, tmp_path: Path, status: str = "active") -> None:
        self.tmp = tmp_path
        self.workdir = tmp_path / "work"
        self.project = self.workdir / "docs" / "projects" / "tinytodo-quick"
        self.project.mkdir(parents=True)
        self.state = tmp_path / "specflo-state.json"
        self.state.write_text(json.dumps({"status": status, "level": "quick", "dir": str(self.project)}))
        self.specflo = str(_script(tmp_path / "bin" / "specflo", STUB_SPECFLO))
        self.harness = str(_script(tmp_path / "harness.py", HARNESS))
        self.run_dir = tmp_path / "run"
        self.pids = tmp_path / "pids.json"
        self.env = {
            "PATH": os.environ["PATH"], "STUB_SPECFLO_STATE": str(self.state),
            "HARNESS_PIDS": str(self.pids), "HARNESS_STDIN": str(tmp_path / "stdin.txt"),
            "HARNESS_SETTLED": str(tmp_path / "settled.txt"), "HARNESS_ENV": str(tmp_path / "env.json"),
        }

    def pi(self, mode: str) -> lc.HarnessSpec:
        agent_dir = self.run_dir / launch_pi.AGENT_DIR_NAME
        launch = launch_pi.PiLaunch(
            argv=[sys.executable, self.harness, mode], env=dict(self.env), cwd=self.workdir,
            agent_dir=agent_dir, pi_version="test", config_hash="sha256:test",
        )
        return lc.pi_spec(launch, self.specflo, self.run_dir)

    def plain(self, mode: str) -> lc.HarnessSpec:
        return lc.HarnessSpec(harness="test", argv=[sys.executable, self.harness, mode],
                              env=dict(self.env), cwd=self.workdir)

    def cc(self, mode: str) -> lc.HarnessSpec:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        spec = lc.cc_spec(self.run_dir / lc.CC_SPEC_FILE, dict(self.env), self.run_dir)
        return replace(spec, argv=[sys.executable, self.harness, mode])

    def supervise(self, spec: lc.HarnessSpec, wall_clock: float = 20.0, stall: float = 10.0, **kw) -> lc.Outcome:
        kw = {"poll": 0.05, "stop_grace": 1.0, **kw}
        return lc.supervise(spec, run_dir=self.run_dir, limits=lc.Limits(wall_clock, stall), **kw)

    def started(self) -> list[int]:
        return json.loads(self.pids.read_text())


# -- end conditions ---------------------------------------------------------------


def test_a_run_whose_project_completes_ends_complete_and_leaves_nothing(tmp_path: Path):
    bench = Bench(tmp_path)
    outcome = bench.supervise(bench.pi("complete"))
    assert outcome.end_reason == lc.END_COMPLETE
    assert outcome.harness_end_reason == "project-complete"
    assert outcome.survivors == []
    assert _alive(bench.started()) == []
    # The pi arm is sent the extension command that starts the auto chain, once.
    sent = json.loads((tmp_path / "stdin.txt").read_text())
    assert sent["type"] == "prompt" and sent["message"] == "/specflo-continue auto"


def test_a_complete_project_waits_for_the_agent_to_settle(tmp_path: Path):
    bench = Bench(tmp_path)
    outcome = bench.supervise(bench.pi("complete-busy"))
    assert outcome.end_reason == lc.END_COMPLETE
    settled = float((tmp_path / "settled.txt").read_text())
    assert datetime.fromisoformat(outcome.ended_at).timestamp() >= settled


def test_an_ended_auto_run_ends_escalated_with_specflos_notice(tmp_path: Path):
    bench = Bench(tmp_path)
    outcome = bench.supervise(bench.pi("escalate"))
    assert outcome.end_reason == lc.END_ESCALATED
    assert outcome.end_detail in ("AUTO-RUN ESCALATION: no forward progress.", "specflo ended the auto run")
    assert _alive(bench.started()) == []


def test_the_kill_switch_ends_escalated(tmp_path: Path):
    bench = Bench(tmp_path)
    outcome = bench.supervise(bench.pi("kill-switch"))
    assert outcome.end_reason == lc.END_ESCALATED
    assert outcome.harness_end_reason == "kill-switch"


def test_a_run_past_its_cap_times_out_and_its_whole_tree_is_removed(tmp_path: Path):
    bench = Bench(tmp_path)
    started = time.monotonic()
    outcome = bench.supervise(bench.plain("busy"), wall_clock=1.5, stall=10.0)
    assert time.monotonic() - started < 10
    assert outcome.end_reason == lc.END_TIMEOUT
    pids = bench.started()
    assert len(pids) == 4  # the harness, a child, a child in its own session, an orphan
    assert outcome.survivors == []
    assert _alive(pids) == []


def test_a_silent_run_stalls_and_its_tree_is_removed(tmp_path: Path):
    bench = Bench(tmp_path)
    outcome = bench.supervise(bench.plain("silent"), wall_clock=30.0, stall=0.5)
    assert outcome.end_reason == lc.END_STALLED
    assert outcome.duration < 10
    assert _alive(bench.started()) == []


def test_an_extra_probe_counts_as_model_activity(tmp_path: Path):
    bench = Bench(tmp_path)
    ticks = iter(range(10**9))
    outcome = bench.supervise(bench.plain("silent"), wall_clock=1.0, stall=0.4, probes=[lambda: next(ticks)])
    assert outcome.end_reason == lc.END_TIMEOUT


def test_file_growth_probe_sees_a_growing_file(tmp_path: Path):
    log = tmp_path / "llama-swap.log"
    probe = lc.file_growth(log)
    assert probe() is None
    log.write_text("a\n")
    first = probe()
    log.write_text("a\nb\n")
    assert probe() != first


def test_a_harness_that_exits_by_itself_ends_harness_exit(tmp_path: Path):
    bench = Bench(tmp_path)
    outcome = bench.supervise(bench.plain("exit"))
    assert outcome.end_reason == lc.END_HARNESS_EXIT
    assert outcome.exit_code == 3
    assert "code 3" in outcome.end_detail
    assert Path(outcome.stdout_log).read_text() == "bye\n"


# -- the Claude Code arm ------------------------------------------------------------


@pytest.mark.parametrize("loop_reason, end_reason", [
    ("project-complete", lc.END_COMPLETE),
    ("stall", lc.END_ESCALATED),
    ("pass-cap", lc.END_ESCALATED),
    ("kill-switch", lc.END_ESCALATED),
    ("review-budget", lc.END_ESCALATED),
    ("ladder-blocked", lc.END_ESCALATED),
    (cc_continue.END_PASS_LIMIT, lc.END_ESCALATED),
    (cc_continue.END_WALL_CLOCK, lc.END_TIMEOUT),
    (cc_continue.END_PASS_FAILED, lc.END_HARNESS_EXIT),
    (cc_continue.END_AUTO_FAILED, lc.END_HARNESS_EXIT),
])
def test_claude_code_loop_ends_map_to_run_ends(loop_reason: str, end_reason: str):
    ending = lc.cc_ending({"result": {"end_reason": loop_reason, "end_detail": "d"}})
    assert ending.reason == end_reason
    assert ending.harness_reason == loop_reason


@pytest.mark.parametrize("mode, end_reason", [
    ("cc-project-complete", lc.END_COMPLETE),
    ("cc-stall", lc.END_ESCALATED),
    ("cc-pass-failed", lc.END_HARNESS_EXIT),
    ("cc-none", lc.END_HARNESS_EXIT),
])
def test_the_claude_code_child_result_decides_the_end(tmp_path: Path, mode: str, end_reason: str):
    bench = Bench(tmp_path)
    outcome = bench.supervise(bench.cc(mode))
    assert outcome.end_reason == end_reason
    if mode != "cc-none":
        assert outcome.end_detail == f"detail of {mode[3:]}"
        assert outcome.harness_report == {"harness": "claude-code test"}


def test_claude_code_pass_streams_count_as_activity(tmp_path: Path):
    bench = Bench(tmp_path)
    outcome = bench.supervise(bench.cc("cc-streams"), wall_clock=1.0, stall=0.4)
    assert outcome.end_reason == lc.END_TIMEOUT
    assert Path(outcome.stdout_log).read_text() == ""


def test_the_claude_code_child_runs_the_loop_with_the_run_settings(tmp_path: Path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    arm = arms.Run(entry=ENTRY, harness="claude-code", level="quick", run_index=0)
    spec = lc.build_cc(arm, workdir=tmp_path / "work", run_dir=run_dir, specflo="/x/specflo",
                       env={"PATH": "/usr/bin"}, limits=lc.Limits(90.0, 30.0),
                       settings=lc.AutoSettings("autonomous", 7))
    assert spec.cwd == run_dir
    seen = {}

    def fake_run(entry, **kw):
        seen.update(kw, entry=entry)
        return cc_continue.RunResult("project-complete", "done"), {"harness": "claude-code 9"}

    monkeypatch.setattr(cc_continue, "run", fake_run)
    assert lc.cc_child_main(spec.argv[-1]) == 0
    assert seen["entry"] == ENTRY and seen["max_passes"] == 7 and seen["wall_clock"] == 90.0
    assert seen["specflo"] == "/x/specflo" and seen["workdir"] == str(tmp_path / "work")
    written = json.loads((run_dir / lc.CC_RESULT_FILE).read_text())
    assert lc.cc_ending(written).reason == lc.END_COMPLETE
    assert spec.report() == {"harness": "claude-code 9"}


def test_a_failing_claude_code_loop_records_its_error(tmp_path: Path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    spec_file = run_dir / lc.CC_SPEC_FILE
    spec_file.write_text(json.dumps({"entry": ENTRY, "workdir": "/w", "run_dir": str(run_dir),
                                     "specflo": "s", "max_passes": 1, "wall_clock": 1.0}))

    def broken(entry, **kw):
        raise launch_pi.LaunchError("no claude here")

    monkeypatch.setattr(cc_continue, "run", broken)
    assert lc.cc_child_main(str(spec_file)) == 1
    ending = lc.cc_ending(json.loads((run_dir / lc.CC_RESULT_FILE).read_text()))
    assert ending.reason == lc.END_HARNESS_EXIT and "no claude here" in ending.detail


# -- herdr ---------------------------------------------------------------------------


def _fake_herdr(tmp_path: Path, monkeypatch, fail: str = "") -> tuple[HerdrAdapter, Path]:
    log = tmp_path / "herdr-calls.jsonl"
    monkeypatch.setenv("FAKE_HERDR_LOG", str(log))
    monkeypatch.setenv("FAKE_HERDR_FAIL", fail)
    return HerdrAdapter(binary=str(_script(tmp_path / "herdr-bin" / "herdr", FAKE_HERDR))), log


def test_with_herdr_the_run_gets_a_pane_and_records_its_id(tmp_path: Path, monkeypatch):
    bench = Bench(tmp_path)
    herdr, log = _fake_herdr(tmp_path, monkeypatch)
    outcome = bench.supervise(bench.pi("complete"), herdr=herdr, label="pi quick 0")
    assert outcome.end_reason == lc.END_COMPLETE
    assert outcome.placement == lc.PLACEMENT_HERDR
    assert outcome.pane_id == "w1:p7"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["workspace", "create", "--label", lc.HERDR_WORKSPACE, "--no-focus"] in calls
    (create,) = [c for c in calls if c[:2] == ["tab", "create"]]
    assert create[create.index("--label") + 1] == "pi quick 0"
    (pane_run,) = [c for c in calls if c[:2] == ["pane", "run"]]
    assert pane_run[2] == "w1:p7"
    assert f"--pid={outcome.pid}" in pane_run[3] and outcome.stdout_log in pane_run[3]
    assert calls[-1] == ["tab", "close", "w1:t1"]


def test_without_herdr_the_run_completes_headless(tmp_path: Path, capsys):
    bench = Bench(tmp_path)
    missing = HerdrAdapter(binary=str(tmp_path / "no-such-herdr"))
    outcome = bench.supervise(bench.pi("complete"), herdr=missing)
    assert outcome.end_reason == lc.END_COMPLETE
    assert outcome.placement == lc.PLACEMENT_HEADLESS
    assert outcome.pane_id is None
    assert outcome.placement_note == "herdr unavailable"
    err = capsys.readouterr().err.strip().splitlines()
    assert err == ["modelbench: herdr unavailable; the run is headless"]


def test_a_failed_placement_falls_back_to_headless(tmp_path: Path, monkeypatch):
    bench = Bench(tmp_path)
    herdr, log = _fake_herdr(tmp_path, monkeypatch, fail="pane run")
    outcome = bench.supervise(bench.pi("complete"), herdr=herdr)
    assert outcome.end_reason == lc.END_COMPLETE
    assert outcome.placement == lc.PLACEMENT_HEADLESS
    assert outcome.placement_note.startswith("herdr placement failed")
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["tab", "close", "w1:t1"] in calls


@pytest.mark.rig
def test_a_live_herdr_pane_follows_the_run(tmp_path: Path):
    herdr = HerdrAdapter()
    if not herdr.available():
        pytest.skip("herdr is not running")
    bench = Bench(tmp_path)
    outcome = bench.supervise(bench.plain("silent"), wall_clock=30.0, stall=2.0, herdr=herdr,
                              label="modelbench lifecycle test")
    assert outcome.placement == lc.PLACEMENT_HERDR and outcome.pane_id
    assert outcome.end_reason == lc.END_STALLED


# -- settings, env and the record -------------------------------------------------------


def test_level_limits_come_from_the_level_settings(tmp_path: Path):
    config = arms.Config(entries={}, harnesses=arms.HARNESSES,
                         levels={"quick": {"wall_clock": 600, "stall_limit": 120}, "fast": {}})
    assert lc.level_limits("quick", config=config) == lc.Limits(600.0, 120.0)
    assert lc.level_limits("quick", config=config, stall=5) == lc.Limits(600.0, 5.0)
    with pytest.raises(lc.LifecycleError, match=r"levels\.fast\.wall_clock"):
        lc.level_limits("fast", config=config)


def test_auto_settings_are_checked():
    with pytest.raises(lc.LifecycleError, match="autonomy"):
        lc.AutoSettings("reckless", 5)
    with pytest.raises(lc.LifecycleError, match="pass_cap"):
        lc.AutoSettings("safe", 0)


def test_the_harness_env_puts_specflo_first_and_drops_the_bench_venv(tmp_path: Path):
    venv_bin = str(wd.REPO / ".venv" / "bin")
    base = {"PATH": os.pathsep.join([venv_bin, "/usr/bin", "/bin"]), "VIRTUAL_ENV": str(wd.REPO / ".venv"),
            "HOME": "/home/x"}
    env, report = lc.harness_env(base, tmp_path / "bin")
    assert env["PATH"].split(os.pathsep) == [str(tmp_path / "bin"), "/usr/bin", "/bin"]
    assert env["SPECFLO_BIN"] == str(tmp_path / "bin" / "specflo")
    assert "VIRTUAL_ENV" not in env and env["HOME"] == "/home/x"
    assert report == {"path_first": str(tmp_path / "bin"), "path_dropped": [venv_bin],
                      "vars_dropped": ["VIRTUAL_ENV"]}


def _env_builder(harness: str):
    def build(arm, *, workdir, run_dir, specflo, env, limits, settings):
        return lc.HarnessSpec(harness=arm.harness, argv=[sys.executable, harness, "env"],
                              env={**env, "HARNESS_PIDS": str(Path(run_dir) / "pids.json"),
                                   "HARNESS_ENV": str(Path(run_dir) / "env.json")},
                              cwd=workdir)
    return build


def test_a_run_records_the_level_autonomy_and_pass_cap(tmp_path: Path):
    sealed = wd.make_workdir(tmp_path / "work", "quick")
    harness = str(_script(tmp_path / "harness.py", HARNESS))
    arm = arms.Run(entry=ENTRY, harness="pi", level="quick", run_index=0)
    run_dir = tmp_path / "run"
    base = dict(os.environ, VIRTUAL_ENV=str(wd.REPO / ".venv"),
                PATH=os.pathsep.join([str(wd.REPO / ".venv" / "bin"), os.environ["PATH"]]))
    outcome = lc.run(
        arm, workdir=sealed.path, run_dir=run_dir, settings=lc.AutoSettings("autonomous", 9),
        limits=lc.Limits(30.0, 10.0), herdr=None, base_env=base, poll=0.05, stop_grace=1.0,
        builders={"pi": _env_builder(harness)},
    )
    assert (outcome.level, outcome.autonomy, outcome.pass_cap) == ("quick", "autonomous", 9)
    assert outcome.end_reason == lc.END_HARNESS_EXIT and outcome.exit_code == 0
    assert outcome.placement == lc.PLACEMENT_HEADLESS
    # specflo in the workdir reads the same settings back.
    for key, value in (("autonomy", "autonomous"), ("auto_max_passes", "9")):
        got = lc._specflo(wd.find_specflo(), ["config", "get", key], sealed.path, os.environ)
        assert got.stdout.strip() == value
    # The harness saw the specflo under test first on PATH, and no bench venv.
    env = json.loads((run_dir / "env.json").read_text())
    path = env["PATH"].split(os.pathsep)
    assert path[0] == str(run_dir / cc_continue.BIN_DIR)
    assert str(wd.REPO / ".venv" / "bin") not in path
    assert "VIRTUAL_ENV" not in env
    assert env["SPECFLO_BIN"] == str(run_dir / cc_continue.BIN_DIR / "specflo")
    saved = json.loads((run_dir / lc.RECORD_FILE).read_text())
    assert saved == outcome.to_dict()
    # The record fields this run supplies pass the run-record schema.
    fields = outcome.record_fields()
    assert fields["settings"] == {"autonomy": "autonomous", "pass_cap": 9}
    assert fields["lifecycle"]["placement"] == "headless"
    rec = {
        "arm": {"entry": ENTRY, "harness": "pi"}, "run_index": 0,
        "versions": {"harness": "pi", "engine_build": "b", "specflo": "s", "entry": ENTRY, "gguf_path": "g"},
        "valid": True, "score": 1.0, "metrics": {}, "diagnostics": {}, "telemetry": {},
        **fields,
        "settings": {"sampling": {}, "context_window": 262144, **fields["settings"]},
    }
    assert record.validate_record(rec) == []


def test_a_workdir_at_another_level_is_refused(tmp_path: Path):
    sealed = wd.make_workdir(tmp_path / "work", "quick")
    arm = arms.Run(entry=ENTRY, harness="pi", level="fast", run_index=0)
    with pytest.raises(lc.LifecycleError, match="level 'quick', not 'fast'"):
        lc.run(arm, workdir=sealed.path, run_dir=tmp_path / "run", settings=lc.AutoSettings("safe", 3),
               limits=lc.Limits(30.0, 10.0), herdr=None, builders={})
