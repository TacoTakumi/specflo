"""The runner: one lease's pi process, started and stopped through ``specflo agent``.

A member's pi lives for one lease. The runner starts it under an agent host
with the launch builder's command line, scoped environment and the lease's
working directory, in a herdr pane named for the member, and raises the lease
wall on the host before it hands the agent's name back. Ending the lease stops
the host and leaves the cause where the former holder's next verb finds it.

pi is the stub, reached through a recorder that writes down what it was
started with. herdr is a fake on PATH that keeps its tabs in a file, really
runs what ``pane run`` is given, and lists a pane for as long as the process
in it lives - which is what ``exec`` in a real pane comes to.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from specflo.agent import lease
from specflo.agent.client import connect
from specflo.agent.statefiles import ENV_STATE_DIR, AgentPaths, read_status
from specflo.errors import SpecfloError
from specflo.pool import launch, piconfig, runner
from specflo.pool.config import Account, Member
from specflo.pool.definitions import AgentDefinition

STUB = Path(__file__).resolve().parents[1] / "agent" / "stub_pi.py"
VENV_BIN = str(Path(sys.executable).parent)

# Stands where pi stands: writes down its command line, environment and
# working directory, then becomes the stub pi on the same pipes.
RECORDER = '''import json, os, sys

record, stub, scenario = sys.argv[1:4]
with open(record, "w", encoding="utf-8") as f:
    json.dump({"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd(),
               "pid": os.getpid()}, f)
os.execv(sys.executable, [sys.executable, stub, scenario])
'''

FAKE_HERDR = '''#!/usr/bin/env python3
import json, os, subprocess, sys

args = sys.argv[1:]
state_file = os.environ["FAKE_HERDR_STATE"]

def load():
    try:
        with open(state_file, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return []

def save(tabs):
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(tabs, f)

def alive(pid):
    if pid is None:
        return True  # an idle shell: nothing was run in the pane yet
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True

def out(result):
    print(json.dumps({"id": "cli", "result": result}))

tabs = [t for t in load() if alive(t["pid"])]
if args[:2] == ["workspace", "list"]:
    out({"workspaces": [{"workspace_id": "wF", "label": "agents"}]})
elif args[:2] == ["tab", "create"]:
    n = len(load()) + 1
    tab = {"tab_id": "wF:t%d" % n, "pane_id": "wF:p%d" % n,
           "label": args[args.index("--label") + 1], "pid": None}
    save(tabs + [tab])
    out({"tab": {"tab_id": tab["tab_id"], "workspace_id": "wF"},
         "root_pane": {"pane_id": tab["pane_id"], "tab_id": tab["tab_id"]}})
elif args[:2] == ["pane", "run"]:
    if not os.environ.get("FAKE_HERDR_NO_RUN"):
        proc = subprocess.Popen(["/bin/sh", "-c", args[3]],
                                stdin=subprocess.DEVNULL,
                                stdout=open(os.environ["FAKE_HERDR_PANE_LOG"], "ab"),
                                stderr=subprocess.STDOUT,
                                start_new_session=True)
        for tab in tabs:
            if tab["pane_id"] == args[2]:
                tab["pid"] = proc.pid
        save(tabs)
    out({"type": "ok"})
elif args[:2] == ["tab", "list"]:
    out({"tabs": [{"tab_id": t["tab_id"], "label": t["label"]} for t in tabs]})
elif args[:2] == ["pane", "list"]:
    out({"panes": [{"pane_id": t["pane_id"], "tab_id": t["tab_id"]} for t in tabs]})
elif args[:2] in (["pane", "report-agent"], ["pane", "release-agent"]):
    out({"type": "ok"})
else:
    sys.stderr.write("unknown command\\n")
    sys.exit(2)
'''

DEFINITION = AgentDefinition(
    name="rebaser",
    role="Rebases the work branch and reports conflicts",
    prompt="You are the rebaser.\nReport every conflict.",
    tools=("read", "bash"),
    deny=("git push",),
    env=("GIT_AUTHOR_NAME",),
)

ACCOUNTS = (
    Account(name="team-a", cap=2, key_env="TEAM_A_KEY"),
    Account(name="team-b", cap=2, key_env="TEAM_B_KEY"),
)

POOL_TOKEN = "pool-token-of-this-daemon"
LEASE_TOKEN = "lease-token-of-the-holder"


def wait_until(cond, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class Rig:
    """The daemon's surroundings: a state directory, a fake herdr, a pi double."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp_path = tmp_path
        self.base = tmp_path / "state"
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.config_root = tmp_path / "piconfig"
        self.record = tmp_path / "record.json"
        self.herdr_state = tmp_path / "herdr.json"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "herdr"
        script.write_text(FAKE_HERDR, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        recorder = tmp_path / "recorder.py"
        recorder.write_text(RECORDER, encoding="utf-8")
        scenario = tmp_path / "scenario.json"
        scenario.write_text(json.dumps({"reply": "done"}), encoding="utf-8")
        self.command = f"{sys.executable} {recorder} {self.record} {STUB} {scenario}"
        # never the real herdr
        monkeypatch.setenv("PATH", ":".join([str(bin_dir), VENV_BIN, "/usr/bin", "/bin"]))
        monkeypatch.setenv(ENV_STATE_DIR, str(self.base))
        monkeypatch.setenv("FAKE_HERDR_STATE", str(self.herdr_state))
        monkeypatch.setenv("FAKE_HERDR_PANE_LOG", str(tmp_path / "pane.log"))
        monkeypatch.setenv("SECRET_X", "not for any member")
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Pool Member")
        monkeypatch.setenv("TEAM_A_KEY", "key-of-team-a")
        monkeypatch.setenv("TEAM_B_KEY", "key-of-team-b")
        monkeypatch.delenv(lease.ENV_LEASE_TOKEN, raising=False)

    def local_member(self) -> Member:
        return Member(
            name="local-1", command=self.command, backing="local",
            labels=(), capacity=1, egress="local", model="tc3",
        )

    def hosted_member(self) -> Member:
        return Member(
            name="hosted-1", command=self.command, backing="hosted",
            labels=(), capacity=1, egress="no-train",
            model="some-vendor/some-model", account="team-a",
        )

    def start(self, member: Member, **kwargs) -> str:
        return runner.start(
            DEFINITION, member, ACCOUNTS,
            cwd=self.work, pool_token=POOL_TOKEN, lease_token=LEASE_TOKEN,
            config_root=self.config_root, **kwargs,
        )

    def recorded(self) -> dict:
        assert wait_until(self.record.is_file), "the member's pi never started"
        assert wait_until(lambda: self.record.read_text(encoding="utf-8").endswith("}"))
        return json.loads(self.record.read_text(encoding="utf-8"))

    def pane_names(self) -> list[str]:
        """The labels of the tabs that hold a listed pane."""
        def herdr(*args):
            done = subprocess.run(["herdr", *args], capture_output=True, text=True, check=True)
            return json.loads(done.stdout)["result"]

        held = {pane["tab_id"] for pane in herdr("pane", "list")["panes"]}
        return [tab["label"] for tab in herdr("tab", "list")["tabs"] if tab["tab_id"] in held]

    def cleanup(self) -> None:
        pids = []
        if self.herdr_state.is_file():
            pids += [tab["pid"] for tab in json.loads(self.herdr_state.read_text())]
        if self.base.is_dir():
            for agent_dir in self.base.iterdir():
                if (agent_dir / "status.json").is_file():
                    snapshot = read_status(agent_dir / "status.json")
                    pids += [snapshot.get("pi_pid"), snapshot.get("host_pid")]
        for pid in pids:
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    yield rig
    rig.cleanup()


def test_start_runs_the_member_as_launched_in_a_pane_named_for_it(rig):
    member = rig.local_member()

    name = rig.start(member)

    assert name == "local-1"
    assert rig.pane_names() == ["local-1"]
    with connect(name) as client:
        status = client.status()["status"]
    # a broker host's record: the rpc transport has a host process, and its
    # record carries no transport field
    assert status.get("transport", "rpc") == "rpc"
    assert status["host_pid"]
    assert status["herdr_pane"] == "wF:p1"

    recorded = rig.recorded()
    # python drops the interpreter from argv; the rest is the builder's line
    assert recorded["argv"] == launch.pi_argv(DEFINITION, member)[1:]
    assert recorded["cwd"] == str(rig.work)
    assert recorded["pid"] == status["pi_pid"]
    env = recorded["env"]
    assert "SECRET_X" not in env
    assert "TEAM_A_KEY" not in env
    assert ENV_STATE_DIR not in env
    assert env["GIT_AUTHOR_NAME"] == "Pool Member"
    assert json.loads(env[launch.DENY_ENV]) == ["git push"]
    expected = launch.member_env(DEFINITION, member, ACCOUNTS, os.environ)
    # python may add LC_CTYPE by itself when it coerces the C locale
    assert {k: v for k, v in env.items() if k != "LC_CTYPE"} == {
        k: v for k, v in expected.items() if k != "LC_CTYPE"
    }


def test_start_raises_the_wall_with_the_pool_and_lease_tokens(rig):
    name = rig.start(rig.local_member())

    with connect(name) as client:
        refused = client.request({"type": "status", "lease_token": "someone-else"})
        assert refused["success"] is False
        assert lease.is_wall_refusal(refused["error"])
        admitted = client.request({"type": "status", "lease_token": LEASE_TOKEN})
        assert admitted["success"] is True
        with pytest.raises(RuntimeError):
            client.lease_clear("another-daemon")
        client.lease_clear(POOL_TOKEN)  # the pool token this host was bound with


def test_a_launch_leaves_no_key_or_environment_on_disk_or_command_line(rig):
    member = rig.hosted_member()

    name = rig.start(member)
    rig.recorded()

    state_dir = AgentPaths.resolve(name).root
    for path in state_dir.iterdir():
        if path.is_file():
            assert "key-of-team-a" not in path.read_text(encoding="utf-8", errors="replace")
    assert "key-of-team-a" not in (rig.tmp_path / "pane.log").read_text(errors="replace")
    host_pid = read_status(state_dir / "status.json")["host_pid"]
    cmdline = Path(f"/proc/{host_pid}/cmdline").read_bytes().decode(errors="replace")
    assert "key-of-team-a" not in cmdline
    assert "You are the rebaser" not in cmdline


def test_a_hosted_member_runs_against_its_generated_pi_directory(rig):
    member = rig.hosted_member()

    name = rig.start(member)

    env = rig.recorded()["env"]
    config_dir = Path(env[launch.AGENT_DIR_ENV])
    assert config_dir.parent == rig.config_root
    assert (config_dir / piconfig.MODELS_FILE).is_file()
    assert env["TEAM_A_KEY"] == "key-of-team-a"
    assert "TEAM_B_KEY" not in env

    runner.stop(name, "released", pool_token=POOL_TOKEN)

    assert not config_dir.exists()


def test_stop_ends_the_process_and_the_pane_and_records_the_cause(rig):
    name = rig.start(rig.local_member())
    status = read_status(AgentPaths.resolve(name).status)

    runner.stop(name, "expired", pool_token=POOL_TOKEN)

    assert wait_until(lambda: not pid_alive(status["pi_pid"]))
    assert wait_until(lambda: not pid_alive(status["host_pid"]))
    assert rig.pane_names() == []
    record = lease.read_ended(AgentPaths.resolve(name).root)
    assert record["cause"] == "expired"
    assert record["request_id"] is None


def test_a_preempted_lease_records_the_preempting_request(rig):
    name = rig.start(rig.local_member())

    runner.stop(name, "preempted", pool_token=POOL_TOKEN, request_id="req-7")

    record = lease.read_ended(AgentPaths.resolve(name).root)
    assert (record["cause"], record["request_id"]) == ("preempted", "req-7")


def test_the_next_lease_gets_a_fresh_process_and_no_ended_record(rig):
    member = rig.local_member()
    name = rig.start(member)
    first = rig.recorded()["pid"]
    runner.stop(name, "released", pool_token=POOL_TOKEN)
    rig.record.unlink()

    assert rig.start(member) == name

    assert rig.recorded()["pid"] != first
    assert lease.read_ended(AgentPaths.resolve(name).root) is None
    assert rig.pane_names() == ["local-1"]


def test_a_member_that_is_already_running_is_refused_and_left_alone(rig):
    member = rig.local_member()
    name = rig.start(member)

    with pytest.raises(runner.RunnerError, match="local-1"):
        rig.start(member)

    with connect(name) as client:
        assert client.request({"type": "status", "lease_token": LEASE_TOKEN})["success"]


def test_a_start_whose_host_never_answers_is_cleaned_up_and_raises(rig, monkeypatch):
    # the pane takes the command and runs nothing, so no host ever serves
    monkeypatch.setenv("FAKE_HERDR_NO_RUN", "1")

    with pytest.raises(runner.RunnerError, match="hosted-1") as raised:
        rig.start(rig.hosted_member(), timeout=2.0)

    assert isinstance(raised.value, SpecfloError)
    assert not rig.record.exists()
    assert list(rig.config_root.iterdir()) == []
    state_dir = AgentPaths.resolve("hosted-1").root
    leftovers = [p.name for p in state_dir.iterdir()] if state_dir.is_dir() else []
    assert leftovers == []


def test_a_member_that_cannot_be_launched_starts_nothing(rig, monkeypatch):
    monkeypatch.delenv("TEAM_A_KEY")

    with pytest.raises(launch.LaunchError, match="TEAM_A_KEY"):
        rig.start(rig.hosted_member())

    assert rig.pane_names() == []
    assert not rig.config_root.exists() or list(rig.config_root.iterdir()) == []


def test_stopping_a_member_whose_host_is_gone_still_records_the_cause(rig):
    runner.stop("local-1", "expired", pool_token=POOL_TOKEN)

    assert lease.read_ended(AgentPaths.resolve("local-1").root)["cause"] == "expired"
