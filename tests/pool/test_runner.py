"""The runner: one lease's pi process, started and stopped through ``specflo agent``.

A member's pi lives for one lease. The runner starts it under an agent host
with the launch builder's command line, scoped environment and the lease's
working directory, in a herdr pane named for the member, and raises the lease
wall on the host before it hands the agent's name back. Ending the lease stops
the host and leaves the cause where the former holder's next verb finds it.
A member whose pi does not start is not handed back: the start fails with what
pi said, and leaves nothing of the launch behind.

pi is the stub, reached through a recorder that writes down what it was
started with. herdr is a fake on PATH that keeps its tabs in a file, really
runs what ``pane run`` is given, and lists a pane for as long as the process
in it lives - which is what ``exec`` in a real pane comes to.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from dataclasses import replace
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

# Stands where a pi stands that does not start: waits as long as it is told,
# says why on its standard error and exits.
FAILING_PI = """#!/bin/sh
sleep "$1"
echo "pi: starting up" >&2
echo "Unknown option: --frobnicate" >&2
exit 1
"""

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
# How the pool knows that holder: by the hash of its token.
HOLDER = hashlib.sha256(LEASE_TOKEN.encode()).hexdigest()
# How long the runner watches a started pi, outside these tests.
PI_START_WATCH = runner.PI_START_WATCH


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
        # The stub pi starts, and every start would sleep the watch out: one
        # look is enough here. A test of a pi that does not start watches
        # for as long as the runner really does.
        monkeypatch.setattr(runner, "PI_START_WATCH", 0.0)

    def watch_as_shipped(self, monkeypatch) -> None:
        monkeypatch.setattr(runner, "PI_START_WATCH", PI_START_WATCH)

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

    def failing_command(self, after: str = "0") -> str:
        """A member's command whose pi exits with an error *after* seconds."""
        script = self.tmp_path / "failing-pi"
        script.write_text(FAILING_PI, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return f"{script} {after}"

    def launch_leftovers(self, name: str) -> list[str]:
        """What a launch of *name* writes that must not outlive it."""
        state_dir = AgentPaths.resolve(name).root
        left = [f for f in (runner.LAUNCH_FILE, runner.CONFIG_DIR_FILE)
                if (state_dir / f).exists()]
        if self.config_root.is_dir():
            left += [p.name for p in self.config_root.iterdir()]
        return left

    def start(self, member: Member, **kwargs) -> str:
        return runner.start(
            DEFINITION, member, ACCOUNTS,
            cwd=self.work, pool_token=POOL_TOKEN, lease_token=LEASE_TOKEN,
            config_root=self.config_root, **kwargs,
        )

    def start_by_hand(self, name: str) -> dict:
        """An agent host under *name* that a developer started, as ``specflo
        agent start`` starts one: no pool knows it. Its status record."""
        done = subprocess.run(
            [
                sys.executable, "-c",
                "import sys; from specflo.cli import main; sys.exit(main())",
                "agent", "start", name, "--cwd", str(self.work), "--pi-cmd", self.command,
                "--no-herdr",
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert done.returncode == 0, done.stderr
        return read_status(AgentPaths.resolve(name).status)

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


def test_a_member_whose_command_names_no_binary_is_refused_and_nothing_is_left(
    rig, monkeypatch
):
    rig.watch_as_shipped(monkeypatch)
    member = replace(rig.hosted_member(), command="/nonexistent/pi --mode rpc")

    with pytest.raises(runner.RunnerError, match="member 'hosted-1'") as raised:
        rig.start(member)

    # what the launch shim said of the command, and nothing of what it was given
    assert "/nonexistent/pi" in str(raised.value)
    assert "No such file or directory" in str(raised.value)
    assert "key-of-team-a" not in str(raised.value)
    assert "Traceback" not in str(raised.value)
    # the host that came up for it is gone, and so is its pane
    host_pid = read_status(AgentPaths.resolve("hosted-1").status)["host_pid"]
    assert wait_until(lambda: not pid_alive(host_pid))
    assert rig.pane_names() == []
    assert rig.launch_leftovers("hosted-1") == []


@pytest.mark.parametrize("after", ["0", "0.4"])
def test_a_member_whose_pi_exits_at_once_is_refused_with_the_tail_of_its_error(
    rig, monkeypatch, after
):
    rig.watch_as_shipped(monkeypatch)
    # "0.4": the pi is still up when the host answers and the wall goes up
    member = replace(rig.hosted_member(), command=rig.failing_command(after))
    # the host keeps one error log across leases: a former lease's is not this one's
    state_dir = AgentPaths.resolve("hosted-1").ensure().root
    (state_dir / runner.PI_STDERR_FILE).write_text("an error of a former lease\n")

    with pytest.raises(runner.RunnerError, match="member 'hosted-1'") as raised:
        rig.start(member)

    assert "Unknown option: --frobnicate" in str(raised.value)
    assert "an error of a former lease" not in str(raised.value)
    host_pid = read_status(state_dir / "status.json")["host_pid"]
    assert wait_until(lambda: not pid_alive(host_pid))
    assert rig.pane_names() == []
    assert rig.launch_leftovers("hosted-1") == []
    # the name is free: the member starts once its command is put right
    assert rig.start(rig.hosted_member()) == "hosted-1"
    assert rig.recorded()["pid"]


def test_the_error_reported_for_a_pi_that_did_not_start_carries_no_account_key(
    rig, monkeypatch
):
    rig.watch_as_shipped(monkeypatch)
    script = rig.tmp_path / "leaking-pi"
    script.write_text('#!/bin/sh\necho "cannot sign in with $TEAM_A_KEY" >&2\nexit 1\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    with pytest.raises(runner.RunnerError, match="cannot sign in with") as raised:
        rig.start(replace(rig.hosted_member(), command=str(script)))

    assert "key-of-team-a" not in str(raised.value)


def test_a_grant_on_a_member_whose_pi_did_not_start_ends_the_lease_and_frees_its_slots(
    pool_rig, monkeypatch
):
    pool_rig.watch_as_shipped(monkeypatch)
    broken = replace(pool_rig.hosted_member(), command=pool_rig.failing_command())
    svc = pool_rig.service(pool_rig.config(broken))

    with pytest.raises(runner.RunnerError, match="Unknown option: --frobnicate"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert store.list_leases(state="active") == []
        (ended,) = store.list_transitions()
    assert ended.kind == "released"
    assert "member 'hosted-1'" in ended.cause
    assert pool_rig.pane_names() == []
    assert pool_rig.launch_leftovers("hosted-1") == []
    # the one slot of the pool, the member, its account and its model is free:
    # the next request takes the member, once its command is put right
    svc = pool_rig.service(pool_rig.config(pool_rig.hosted_member()))
    assert svc.grant("rebasers", holder_label="b", cwd=pool_rig.work).agent == "hosted-1"


def test_a_member_that_cannot_be_launched_starts_nothing(rig, monkeypatch):
    monkeypatch.delenv("TEAM_A_KEY")

    with pytest.raises(launch.LaunchError, match="TEAM_A_KEY"):
        rig.start(rig.hosted_member())

    assert rig.pane_names() == []
    assert not rig.config_root.exists() or list(rig.config_root.iterdir()) == []


def test_stopping_a_member_whose_host_is_gone_still_records_the_cause(rig):
    runner.stop("local-1", "expired", pool_token=POOL_TOKEN)

    assert lease.read_ended(AgentPaths.resolve("local-1").root)["cause"] == "expired"


@pytest.mark.parametrize("bound_to", [None, "the-token-of-another-pool"])
def test_stop_leaves_alone_a_host_that_does_not_take_this_pools_token(rig, bound_to):
    # a developer's own agent under a member's name: this pool never started it
    before = rig.start_by_hand("local-1")
    if bound_to is not None:
        with connect("local-1") as client:
            client.pool_bind(bound_to)

    runner.stop("local-1", "released", pool_token=POOL_TOKEN, holder=HOLDER)

    time.sleep(0.5)  # a stop that was sent would have landed by now
    after = read_status(AgentPaths.resolve("local-1").status)
    assert (after["host_pid"], after["pi_pid"]) == (before["host_pid"], before["pi_pid"])
    assert pid_alive(before["host_pid"]) and pid_alive(before["pi_pid"])
    with connect("local-1") as client:
        assert client.status()["status"]["state"] == "idle"
    # and nothing says a lease ended there: none of this pool's ran on it
    state_dir = AgentPaths.resolve("local-1").root
    assert lease.read_ended(state_dir) is None
    assert not (state_dir / lease.ENDED_DIR).exists()


def test_stop_does_not_take_a_host_that_gives_no_answer_for_one_that_is_gone(rig):
    name = rig.start(rig.hosted_member())
    config_dir = Path(rig.recorded()["env"][launch.AGENT_DIR_ENV])
    state_dir = AgentPaths.resolve(name).root
    status = read_status(state_dir / "status.json")
    # a host that hangs: stopped in place, it takes the connection and says nothing
    os.kill(status["host_pid"], signal.SIGSTOP)
    try:
        with pytest.raises(runner.RunnerError, match="member 'hosted-1' did not stop") as raised:
            runner.stop(name, "released", pool_token=POOL_TOKEN, holder=HOLDER, timeout=1.0)

        assert "had no answer from its host" in str(raised.value)
        assert "Traceback" not in str(raised.value)
        # the member may still run, with its key: what it runs on stays
        assert pid_alive(status["host_pid"]) and pid_alive(status["pi_pid"])
        assert config_dir.is_dir()
        assert (state_dir / runner.CONFIG_DIR_FILE).is_file()
        assert read_status(state_dir / "status.json")["state"] != "stopped"
        # the lease has ended all the same, and its holder is told why
        assert lease.read_ended(state_dir, LEASE_TOKEN)["cause"] == "released"
    finally:
        try:
            os.kill(status["host_pid"], signal.SIGCONT)
        except OSError:
            pass  # gone already; the rig's cleanup ends what is left


def test_stop_does_not_take_a_host_that_accepts_no_more_connections_for_one_that_is_gone(rig):
    paths = AgentPaths.resolve("hosted-1").ensure()
    config_dir = piconfig.create(rig.config_root, rig.hosted_member(), ACCOUNTS)
    (paths.root / runner.CONFIG_DIR_FILE).write_text(str(config_dir), encoding="utf-8")
    # a host that hangs for long: it listens, and its queue of callers is full
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    callers = []
    try:
        listener.bind(str(paths.socket))
        listener.listen(0)
        for _ in range(8):
            caller = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            callers.append(caller)
            caller.settimeout(0.2)
            try:
                caller.connect(str(paths.socket))
            except (BlockingIOError, TimeoutError):
                break  # a full queue turns a caller that will not wait for ever away
        else:
            pytest.fail("the listener's queue never filled")

        with pytest.raises(runner.RunnerError, match="member 'hosted-1' did not stop"):
            runner.stop("hosted-1", "expired", pool_token=POOL_TOKEN, timeout=1.0)

        assert config_dir.is_dir()
        assert (paths.root / runner.CONFIG_DIR_FILE).is_file()
        assert lease.read_ended(paths.root)["cause"] == "expired"
    finally:
        for sock in [listener, *callers]:
            sock.close()


def test_stopping_a_member_whose_socket_refuses_the_connection_finds_it_gone(rig):
    paths = AgentPaths.resolve("hosted-1").ensure()
    config_dir = piconfig.create(rig.config_root, rig.hosted_member(), ACCOUNTS)
    (paths.root / runner.CONFIG_DIR_FILE).write_text(str(config_dir), encoding="utf-8")
    # what a killed host leaves: a socket file that nothing listens on
    left = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    left.bind(str(paths.socket))
    left.close()

    runner.stop("hosted-1", "expired", pool_token=POOL_TOKEN)

    assert lease.read_ended(paths.root)["cause"] == "expired"
    assert rig.launch_leftovers("hosted-1") == []


def test_a_probe_that_this_process_could_not_make_does_not_say_the_host_is_gone(
    rig, monkeypatch
):
    name = rig.start(rig.hosted_member())
    status = read_status(AgentPaths.resolve(name).status)

    def no_socket_to_be_had(*args, **kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(runner, "connect", no_socket_to_be_had)

    # the wall still stands, so the stop verb is turned away: the host runs on
    with pytest.raises(runner.RunnerError, match="member 'hosted-1' did not stop"):
        runner.stop(name, "released", pool_token=POOL_TOKEN, timeout=5.0)

    assert pid_alive(status["host_pid"]) and pid_alive(status["pi_pid"])
    assert rig.launch_leftovers(name) != []
