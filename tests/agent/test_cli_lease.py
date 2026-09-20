"""The agent verbs under a lease: they present the holder's token, and they
say why a lease ended once its host is gone."""

from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo.agent import lease
from specflo.agent.cli import EXIT_UNREACHABLE, agent_app
from specflo.agent.client import connect
from specflo.agent.host import PiHost
from specflo.agent.statefiles import ENV_STATE_DIR, AgentPaths, read_status

STUB = Path(__file__).parent / "stub_pi.py"

POOL = "pool-secret"
HOLDER = "holder-secret"
REPLY = "the member's reply"

# verb -> the arguments that follow the agent name
VERBS = {
    "prompt": ["intruder text"],
    "wait": [],
    "last": [],
    "log": [],
    "status": [],
    "stop": [],
}


def captured(path: Path) -> list[dict]:
    """Every frame the stub pi received on stdin."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def run(*args: str):
    return CliRunner().invoke(agent_app, list(args))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """In-process hosts under a private state dir, and a clean client root."""
    base = tmp_path / "state"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(ENV_STATE_DIR, str(base))
    monkeypatch.delenv(lease.ENV_LEASE_TOKEN, raising=False)
    monkeypatch.chdir(work)
    hosts = []

    def make(name: str, leased: bool = True):
        capture = tmp_path / f"capture-{name}.jsonl"
        scenario_file = tmp_path / f"scenario-{name}.json"
        scenario_file.write_text(
            json.dumps({"reply": REPLY, "capture": str(capture)}), encoding="utf-8"
        )
        host = (
            PiHost(
                name,
                [sys.executable, str(STUB), str(scenario_file)],
                cwd=tmp_path,
                base_dir=base,
            )
            .start()
            .serve()
        )
        hosts.append(host)
        if leased:
            # the way the pool daemon raises the wall at grant
            with connect(name, base_dir=base) as daemon:
                daemon.pool_bind(POOL)
                daemon.lease_bind(POOL, HOLDER)
        return host, capture

    yield make, base, work
    for host in hosts:
        host.close()


# -- finding the token ------------------------------------------------------


def write_token_file(root: Path, agent: str, token: str) -> Path:
    path = root / ".specflo" / "leases" / f"{agent}.token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    return path


def test_token_comes_from_the_option_first(tmp_path):
    write_token_file(tmp_path, "m1", "from-file")
    found = lease.find_token(
        "m1",
        option="from-option",
        environ={lease.ENV_LEASE_TOKEN: "from-env"},
        start=tmp_path,
    )
    assert found == "from-option"


def test_token_comes_from_the_environment_before_the_file(tmp_path):
    write_token_file(tmp_path, "m1", "from-file")
    found = lease.find_token(
        "m1", environ={lease.ENV_LEASE_TOKEN: "from-env"}, start=tmp_path
    )
    assert found == "from-env"


def test_token_file_is_found_upward_from_the_working_directory(tmp_path):
    write_token_file(tmp_path, "m1", "from-file")
    write_token_file(tmp_path, "other", "not-this-one")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert lease.find_token("m1", environ={}, start=nested) == "from-file"


def test_the_nearest_token_file_wins(tmp_path):
    write_token_file(tmp_path, "m1", "outer")
    nested = tmp_path / "a"
    write_token_file(nested, "m1", "inner")
    assert lease.find_token("m1", environ={}, start=nested / "b") == "inner"


def test_no_token_anywhere_is_none(tmp_path):
    assert lease.find_token("m1", environ={}, start=tmp_path) is None
    # an empty option, variable or file is no token either
    write_token_file(tmp_path, "m1", "")
    assert (
        lease.find_token(
            "m1", option="", environ={lease.ENV_LEASE_TOKEN: ""}, start=tmp_path
        )
        is None
    )


def test_an_agent_name_cannot_walk_out_of_the_leases_directory(tmp_path):
    (tmp_path / ".specflo").mkdir()
    (tmp_path / ".specflo" / "secret.token").write_text("stolen", encoding="utf-8")
    (tmp_path / ".specflo" / "leases").mkdir()
    assert lease.find_token("../secret", environ={}, start=tmp_path) is None


def test_lease_module_is_stdlib_only():
    tree = ast.parse(Path(lease.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            roots.add((node.module or "").split(".")[0])
    assert roots <= set(sys.stdlib_module_names), sorted(
        roots - set(sys.stdlib_module_names)
    )


# -- the ended record -------------------------------------------------------


def test_ended_record_round_trips(tmp_path):
    paths = AgentPaths.resolve("m1", tmp_path).ensure()
    assert paths.ended == paths.root / lease.ENDED_FILE
    assert lease.read_ended(paths.root) is None

    lease.write_ended(paths.root, "expired", ended_at="2026-01-01T00:00:00.000+00:00")
    assert json.loads(paths.ended.read_text()) == {
        "cause": "expired",
        "request_id": None,
        "ended_at": "2026-01-01T00:00:00.000+00:00",
    }
    assert lease.read_ended(paths.root)["cause"] == "expired"

    # a later ending replaces the record, and a new lease clears it
    lease.write_ended(paths.root, "preempted", request_id="rq-7")
    record = lease.read_ended(paths.root)
    assert record["request_id"] == "rq-7"
    assert record["ended_at"]
    lease.clear_ended(paths.root)
    lease.clear_ended(paths.root)
    assert lease.read_ended(paths.root) is None


def test_ended_message_names_the_cause():
    assert lease.ended_message({"cause": "released"}) == "lease released"
    assert lease.ended_message({"cause": "expired"}) == "lease expired"
    assert (
        lease.ended_message({"cause": "preempted", "request_id": "rq-7"})
        == "lease preempted by rq-7"
    )


def test_write_ended_refuses_a_record_it_could_not_report(tmp_path):
    with pytest.raises(ValueError):
        lease.write_ended(tmp_path, "vanished")
    with pytest.raises(ValueError):
        lease.write_ended(tmp_path, "preempted")  # by whom?
    assert lease.read_ended(tmp_path) is None


def test_a_damaged_ended_record_reads_as_none(tmp_path):
    (tmp_path / lease.ENDED_FILE).write_text("{not json", encoding="utf-8")
    assert lease.read_ended(tmp_path) is None
    (tmp_path / lease.ENDED_FILE).write_text('{"cause": "vanished"}', encoding="utf-8")
    assert lease.read_ended(tmp_path) is None


# -- the verbs at the wall --------------------------------------------------


@pytest.mark.parametrize("verb", sorted(VERBS))
@pytest.mark.parametrize(
    "credentials",
    [[], ["--lease-token", "wrong"], ["--lease-token", POOL]],
    ids=["no-token", "wrong-token", "pool-token-as-lease-token"],
)
def test_verb_without_the_right_token_is_refused(rig, verb, credentials):
    make, base, _ = rig
    host, capture = make("m1")
    # the holder has worked, so there is member output a verb could leak
    assert run("prompt", "m1", "holder text", "--lease-token", HOLDER).exit_code == 0
    frames_before = captured(capture)
    events_before = host.paths.events.read_text()

    result = run(verb, "m1", *VERBS[verb], *credentials)

    assert result.exit_code != 0
    assert "lease" in result.stderr
    assert REPLY not in result.output
    assert "holder text" not in result.output
    # pi received nothing and the event log records no input
    assert captured(capture) == frames_before
    assert host.paths.events.read_text() == events_before
    assert "intruder text" not in host.paths.events.read_text()
    # the refused stop did not stop anything
    assert read_status(host.paths.status)["state"] == "idle"
    assert host.proc.poll() is None


def test_wrong_token_from_the_environment_or_a_file_is_refused(rig, monkeypatch):
    make, _, work = rig
    _, capture = make("m2")
    monkeypatch.setenv(lease.ENV_LEASE_TOKEN, "wrong")
    assert run("prompt", "m2", "intruder text").exit_code != 0
    monkeypatch.delenv(lease.ENV_LEASE_TOKEN)
    write_token_file(work, "m2", "wrong")
    assert run("prompt", "m2", "intruder text").exit_code != 0
    assert captured(capture) == []


def test_holder_token_from_the_option(rig):
    make, _, _ = rig
    _, capture = make("m3")
    result = run("prompt", "m3", "go", "--lease-token", HOLDER)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == REPLY + "\n"
    frames = captured(capture)
    assert [f["type"] for f in frames] == ["prompt", "get_last_assistant_text"]
    assert not any("lease_token" in f for f in frames)


def test_holder_token_from_the_environment(rig, monkeypatch):
    make, _, _ = rig
    make("m4")
    monkeypatch.setenv(lease.ENV_LEASE_TOKEN, HOLDER)
    result = run("prompt", "m4", "go")
    assert result.exit_code == 0, result.stderr
    assert result.stdout == REPLY + "\n"


def test_holder_token_from_a_file_above_the_working_directory(rig, monkeypatch):
    make, _, work = rig
    host, _ = make("m5")
    write_token_file(work, "m5", HOLDER)
    nested = work / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    result = run("prompt", "m5", "go")
    assert result.exit_code == 0, result.stderr
    assert result.stdout == REPLY + "\n"

    # the reading verbs pass with the same token
    assert run("wait", "m5", "--timeout", "5").exit_code == 0
    last = run("last", "m5")
    assert last.exit_code == 0 and last.stdout == REPLY + "\n"
    log = run("log", "m5")
    # the holder's log is the lease's part: it begins where the lease was bound
    assert log.exit_code == 0 and '"host_forward"' in log.stdout
    assert host.paths.events.read_text().endswith(log.stdout)
    status = run("status", "m5", "--json")
    assert status.exit_code == 0, status.stderr
    assert json.loads(status.stdout)["state"] == "idle"
    assert HOLDER not in host.paths.events.read_text()


def test_holder_stop_stops_a_detached_host(rig):
    make, base, _ = rig
    scenario_file = base.parent / "scenario-m6.json"
    scenario_file.write_text(json.dumps({"reply": REPLY}), encoding="utf-8")
    # started by a CLI process of its own, so the host is truly detached and
    # not a child this test process would have to reap before it reads as gone
    started = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; from specflo.cli import main; sys.exit(main())",
            "agent", "start", "m6", "--cwd", ".", "--no-herdr",
            "--pi-cmd", f"{sys.executable} {STUB} {scenario_file}",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert started.returncode == 0, started.stderr
    try:
        with connect("m6", base_dir=base) as daemon:
            daemon.pool_bind(POOL)
            daemon.lease_bind(POOL, HOLDER)
        assert run("stop", "m6").exit_code != 0
        assert read_status(base / "m6" / "status.json")["state"] == "idle"
        result = run("stop", "m6", "--lease-token", HOLDER)
        assert result.exit_code == 0, result.stderr
        assert read_status(base / "m6" / "status.json")["state"] == "stopped"
    finally:
        snapshot = read_status(base / "m6" / "status.json")
        for key in ("pi_pid", "host_pid"):
            if snapshot.get(key):
                try:
                    os.kill(snapshot[key], signal.SIGKILL)
                except OSError:
                    pass


def test_verbs_on_an_unleased_host_need_no_token(rig):
    make, _, _ = rig
    host, capture = make("m7", leased=False)
    result = run("prompt", "m7", "go")
    assert result.exit_code == 0, result.stderr
    assert result.stdout == REPLY + "\n"
    assert run("wait", "m7").exit_code == 0
    assert run("status", "m7").exit_code == 0
    assert run("log", "m7").stdout == host.paths.events.read_text()
    # no credential field reaches a pi that no pool ever bound
    assert not any("lease_token" in f for f in captured(capture))


# -- the host is gone: say why the lease ended ------------------------------


@pytest.mark.parametrize("verb", sorted(VERBS))
@pytest.mark.parametrize(
    "ending, message",
    [
        ({"cause": "released"}, "lease released"),
        ({"cause": "expired"}, "lease expired"),
        ({"cause": "preempted", "request_id": "rq-42"}, "lease preempted by rq-42"),
    ],
    ids=["released", "expired", "preempted"],
)
def test_verb_reports_why_the_lease_ended(rig, verb, ending, message):
    make, base, _ = rig
    host, _ = make("m8")
    assert run("prompt", "m8", "go", "--lease-token", HOLDER).exit_code == 0
    # the lease ends the way the pool ends it: the host stops, the cause lands
    with connect("m8", base_dir=base) as daemon:
        assert daemon.request({"type": "stop", "pool_token": POOL})["success"]
    assert host.wait_stopped(timeout=10)
    lease.write_ended(host.paths.root, **ending)

    result = run(verb, "m8", *VERBS[verb], "--lease-token", HOLDER)

    assert result.exit_code != 0
    assert message in result.stderr
    assert REPLY not in result.output


def test_a_gone_host_without_an_ended_record_is_plainly_unreachable(rig):
    _, base, _ = rig
    AgentPaths.resolve("m9", base).ensure()
    result = run("prompt", "m9", "go")
    assert result.exit_code == EXIT_UNREACHABLE
    assert "lease" not in result.stderr
