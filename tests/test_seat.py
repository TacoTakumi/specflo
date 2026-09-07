"""The agent's seat: a client workspace under the daemon root, one per hosted project.

The daemon runs an agent for a hosted project as a CLI client of itself.
A client needs a checkout to run in: a config whose active project is the
hosted slug and a remote entry pointing at the daemon with an agent token.
The seat is that checkout and nothing more: no artifact lives in it, and
scaffolding it again changes nothing.
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.agent.statefiles import ENV_STATE_DIR
from specflo.cli import app
from specflo.daemon import auth, seat
from specflo.errors import SpecfloError
from specflo.service.local import LocalProjectService

runner = CliRunner()

AGENT_TESTS = Path(__file__).parent / "agent"
HARNESS_RUNNER = AGENT_TESTS / "harness_runner.mjs"
STUB_PI = AGENT_TESTS / "stub_pi.py"


def _fake_herdr_script() -> str:
    """The fake herdr the agent suite already uses, loaded from its test module."""
    spec = importlib.util.spec_from_file_location(
        "fake_herdr_source", AGENT_TESTS / "test_herdr_adapter.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FAKE_HERDR


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


def hosted(root, name="Login fix"):
    """A project the daemon holds, with a brainstorm to read back."""
    service = LocalProjectService(root, config.load_config(root), actor="requester", hosted=True)
    project = service.create_project(name)
    service.start_brainstorm(project.slug)
    return project.slug


def test_scaffold_makes_a_client_checkout_bound_to_the_hosted_project(root):
    slug = hosted(root)

    workspace = seat.scaffold(root, slug, "http://127.0.0.1:8741", "agent-secret")

    assert workspace == seat.seat_dir(root, slug) == root / seat.SEATS_DIRNAME / slug
    cfg = config.load_config(workspace)
    assert cfg.active_project == slug
    assert config.hosting_remote(workspace, slug) == seat.REMOTE_NAME
    remote = config.load_remote(workspace, seat.REMOTE_NAME)
    assert remote.url == "http://127.0.0.1:8741" and remote.token == "agent-secret"
    assert config.list_remotes(workspace) == {seat.REMOTE_NAME: "http://127.0.0.1:8741"}


def test_the_seat_holds_no_projects_directory_and_no_artifact(root):
    slug = hosted(root)

    workspace = seat.scaffold(root, slug, "http://127.0.0.1:8741", "agent-secret")

    cfg = config.load_config(workspace)
    assert not (workspace / cfg.projects_dir).exists()
    assert not (workspace / "docs").exists()
    entries = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*") if p.is_file())
    assert entries == [
        ".specflo/config.yaml",
        ".specflo/hosted.json",
        ".specflo/remotes/.gitignore",
        f".specflo/remotes/{seat.REMOTE_NAME}.json",
    ]


def test_scaffolding_twice_leaves_one_identical_workspace(root):
    slug = hosted(root)
    first = seat.scaffold(root, slug, "http://127.0.0.1:8741", "agent-secret")
    snapshot = {p.relative_to(first).as_posix(): p.read_bytes() for p in first.rglob("*") if p.is_file()}

    again = seat.scaffold(root, slug, "http://127.0.0.1:8741", "agent-secret")

    assert again == first
    assert {p.relative_to(again).as_posix(): p.read_bytes() for p in again.rglob("*") if p.is_file()} == snapshot
    assert [p.name for p in (root / seat.SEATS_DIRNAME).iterdir()] == [slug]


def test_scaffolding_again_with_a_new_token_or_url_rebinds_the_remote(root):
    slug = hosted(root)
    seat.scaffold(root, slug, "http://127.0.0.1:8741", "old-secret")

    workspace = seat.scaffold(root, slug, "http://127.0.0.1:9000", "new-secret")

    remote = config.load_remote(workspace, seat.REMOTE_NAME)
    assert remote.url == "http://127.0.0.1:9000" and remote.token == "new-secret"


def test_the_token_is_kept_out_of_git_and_readable_by_the_owner_only(root):
    slug = hosted(root)

    workspace = seat.scaffold(root, slug, "http://127.0.0.1:8741", "agent-secret")

    remotes = workspace / ".specflo" / "remotes"
    assert (remotes / ".gitignore").read_text() == "*\n"
    assert (remotes / f"{seat.REMOTE_NAME}.json").stat().st_mode & 0o777 == 0o600


def test_scaffold_refuses_a_slug_that_is_not_one(root):
    from specflo.errors import SpecfloError

    with pytest.raises(SpecfloError):
        seat.scaffold(root, "../escape", "http://127.0.0.1:8741", "agent-secret")
    assert not (root / seat.SEATS_DIRNAME).exists()


def test_a_cli_run_from_the_seat_resolves_the_project_as_hosted(root, live_daemon, monkeypatch):
    slug = hosted(live_daemon["root"])
    token = auth.mint_token(live_daemon["root"], "agent")
    workspace = seat.scaffold(live_daemon["root"], slug, live_daemon["url"], token)
    monkeypatch.chdir(workspace)

    status = runner.invoke(app, ["status", "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["active_project"] == slug

    shown = runner.invoke(app, ["doc", "show", "brainstorm"])
    assert shown.exit_code == 0, shown.output
    expected = (live_daemon["root"] / daemon.PROJECTS_DIRNAME / slug / "brainstorm.md").read_text()
    assert shown.stdout == expected

    whoami = runner.invoke(app, ["decision", "add", "--text", "From the seat"])
    assert whoami.exit_code == 0, whoami.output
    assert "- Actor: agent" in (live_daemon["root"] / daemon.PROJECTS_DIRNAME / slug / "brainstorm.md").read_text()
    assert not (workspace / "docs").exists()


# --- starting the agent -------------------------------------------------------


def wait_until(cond, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def agent_rig(tmp_path, monkeypatch):
    """An isolated agent state dir and a fake herdr on PATH, in this process's env.

    The daemon starts agents by running the agent CLI as a subprocess that
    inherits the environment, so the rig sets it on the process.
    """
    base = tmp_path / "state"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "herdr"
    script.write_text(_fake_herdr_script(), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "herdr-calls.jsonl"
    monkeypatch.setenv(ENV_STATE_DIR, str(base))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HERDR_LOG", str(log))
    for key in ("SPECFLO_AGENT_SERVE", "SPECFLO_AGENT_NAME", "SPECFLO_AGENT_MANAGED", "SPECFLO_AGENT_START_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    procs = []

    def herdr_calls():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().split("\n") if line]

    def serve_socket(name):
        """The harness runner stands in for pi-in-the-pane: it binds the socket."""
        scenario = tmp_path / f"scenario-{name}.json"
        scenario.write_text(json.dumps({"reply": "served"}), encoding="utf-8")
        proc = subprocess.Popen(
            ["node", str(HARNESS_RUNNER), str(scenario)],
            cwd=tmp_path,
            env={**os.environ, "SPECFLO_AGENT_NAME": name, "SPECFLO_AGENT_MANAGED": "1"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        return proc

    def stub_cmd(tag):
        scenario = tmp_path / f"scenario-{tag}.json"
        scenario.write_text(json.dumps({"reply": "ok"}), encoding="utf-8")
        return f"{sys.executable} {STUB_PI} {scenario}"

    def agent_cli(*args):
        return subprocess.run(
            [sys.executable, "-c", "import sys; from specflo.cli import main; sys.exit(main())", "agent", *args],
            capture_output=True, text=True, env=dict(os.environ), timeout=30,
        )

    yield {"herdr_calls": herdr_calls, "serve_socket": serve_socket, "stub_cmd": stub_cmd, "cli": agent_cli, "base": base}

    for name in seat.agent_mapping(tmp_path / "daemon").values():
        agent_cli("stop", name)
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def scaffolded(root, slug="login-fix"):
    hosted(root, name=slug.replace("-", " ").title())
    seat.scaffold(root, slug, "http://127.0.0.1:8741", "agent-secret")
    return slug


def test_the_agent_name_derives_from_the_slug_and_the_mapping_starts_empty(root):
    assert seat.agent_name("login-fix") == "project-login-fix"
    assert seat.agent_mapping(root) == {}
    assert seat.agent_for(root, "login-fix") is None


def test_start_agent_needs_a_scaffolded_seat(root):
    hosted(root)

    with pytest.raises(SpecfloError, match="seat"):
        seat.start_agent(root, "login-fix")
    assert seat.agent_mapping(root) == {}


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node serves the harness socket")


@needs_node
def test_start_agent_runs_a_tui_agent_in_the_seat_and_records_the_mapping(root, agent_rig):
    slug = scaffolded(root)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        started = pool.submit(seat.start_agent, root, slug)
        assert wait_until(lambda: any(call[:2] == ["pane", "run"] for call in agent_rig["herdr_calls"]()), timeout=20)
        agent_rig["serve_socket"](seat.agent_name(slug))
        name = started.result(timeout=40)

    assert name == "project-login-fix"
    assert seat.agent_mapping(root) == {slug: name}
    assert seat.agent_for(root, slug) == name
    pane_runs = [call for call in agent_rig["herdr_calls"]() if call[:2] == ["pane", "run"]]
    assert len(pane_runs) == 1
    command = pane_runs[0][3]
    assert f"SPECFLO_AGENT_NAME={name}" in command
    assert command.rstrip().endswith(" pi")
    listed = json.loads(agent_rig["cli"]("list", "--json").stdout)
    row = next(item for item in listed if item["name"] == name)
    assert row["alive"] is True and row["transport"] == "tui"
    # The pane was created in the seat: the tab's cwd argument is the workspace.
    tab_creates = [call for call in agent_rig["herdr_calls"]() if call[:2] == ["tab", "create"]]
    assert tab_creates and str(seat.seat_dir(root, slug)) in tab_creates[0]


@needs_node
def test_a_start_whose_socket_never_connects_fails_and_records_nothing(root, agent_rig, monkeypatch):
    slug = scaffolded(root)
    monkeypatch.setenv("SPECFLO_AGENT_START_TIMEOUT", "1")

    with pytest.raises(seat.AgentStartError):
        seat.start_agent(root, slug)

    assert seat.agent_mapping(root) == {}
    assert any(call[:2] == ["tab", "close"] for call in agent_rig["herdr_calls"]())
    listed = json.loads(agent_rig["cli"]("list", "--json").stdout)
    assert not any(item["name"] == seat.agent_name(slug) and item["alive"] for item in listed)


def test_the_rpc_switch_selects_the_rpc_transport(root, agent_rig):
    slug = scaffolded(root)
    config.write_value(root, config.field_for("agent_transport"), "rpc")

    name = seat.start_agent(root, slug, pi_cmd=agent_rig["stub_cmd"]("rpc"))

    assert seat.agent_mapping(root) == {slug: name}
    listed = json.loads(agent_rig["cli"]("list", "--json").stdout)
    row = next(item for item in listed if item["name"] == name)
    assert row["alive"] is True and row["transport"] == "rpc"
    assert not any(call[:2] == ["pane", "run"] for call in agent_rig["herdr_calls"]())


def test_the_mapping_is_a_file_under_the_root_that_survives_a_reload(root):
    seat.record_agent(root, "login-fix", "project-login-fix")
    seat.record_agent(root, "dark-mode", "project-dark-mode")

    assert seat.agents_path(root) == root / seat.AGENTS_FILENAME
    assert json.loads(seat.agents_path(root).read_text()) == {
        "dark-mode": "project-dark-mode", "login-fix": "project-login-fix",
    }
    assert seat.forget_agent(root, "login-fix") is True
    assert seat.forget_agent(root, "login-fix") is False
    assert seat.agent_mapping(root) == {"dark-mode": "project-dark-mode"}


# --- the agent's end: the advance out of brainstorm, or a developer's stop ------


def test_stop_agent_stops_the_recorded_agent_and_clears_the_mapping(root, agent_rig):
    slug = scaffolded(root)
    config.write_value(root, config.field_for("agent_transport"), "rpc")
    name = seat.start_agent(root, slug, pi_cmd=agent_rig["stub_cmd"]("rpc"))
    assert seat.liveness(root, slug).alive is True

    assert seat.stop_agent(root, slug) == name

    assert seat.agent_mapping(root) == {}
    assert seat.liveness(root, slug) == seat.Liveness(name=None, alive=False, state=seat.MISSING_STATE)
    listed = json.loads(agent_rig["cli"]("list", "--json").stdout)
    assert not any(item["name"] == name and item["alive"] for item in listed)


def test_stop_agent_with_none_on_record_changes_nothing(root):
    slug = scaffolded(root)
    seat.record_agent(root, "other", "project-other")

    assert seat.stop_agent(root, slug) is None

    assert seat.agent_mapping(root) == {"other": "project-other"}


def test_an_agent_already_gone_is_forgotten_all_the_same(root, agent_rig):
    slug = scaffolded(root)
    config.write_value(root, config.field_for("agent_transport"), "rpc")
    name = seat.start_agent(root, slug, pi_cmd=agent_rig["stub_cmd"]("rpc"))
    assert agent_rig["cli"]("stop", name).returncode == 0

    assert seat.stop_agent(root, slug) == name

    assert seat.agent_mapping(root) == {}


def daemon_client(root):
    from fastapi.testclient import TestClient

    from specflo.daemon.app import create_app

    client = TestClient(create_app(root))
    client.headers["Authorization"] = f"Bearer {auth.mint_token(root, 'developer')}"
    return client


def advance(client, slug):
    from specflo.service import wire

    response = client.post(wire.route_path("advance_project"), json={"slug": slug})
    assert response.status_code == 200, response.text
    return wire.decode(response.json()["result"], wire.OPERATIONS["advance_project"].returns)


def audit_records(root):
    from specflo.daemon import routes

    path = root / routes.AUDIT_FILENAME
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


def test_advancing_out_of_brainstorm_over_the_daemon_stops_the_agent_and_clears_the_mapping(root, agent_rig):
    slug = scaffolded(root)
    config.write_value(root, config.field_for("agent_transport"), "rpc")
    name = seat.start_agent(root, slug, pi_cmd=agent_rig["stub_cmd"]("rpc"))

    project = advance(daemon_client(root), slug)

    assert project.phase == "spec"
    assert seat.agent_mapping(root) == {}
    listed = json.loads(agent_rig["cli"]("list", "--json").stdout)
    assert not any(item["name"] == name and item["alive"] for item in listed)
    operations = [(record["identity"], record["operation"], record["project"]) for record in audit_records(root)]
    assert operations[-2:] == [("developer", "advance_project", slug), ("developer", "stop_agent", slug)]


def test_advancing_a_project_past_brainstorm_touches_no_agent(root, agent_rig):
    slug = scaffolded(root)
    seat.record_agent(root, "other", "project-other")
    client = daemon_client(root)
    advance(client, slug)
    LocalProjectService(root, config.load_config(root), hosted=True).start_spec(slug)
    before = audit_records(root)

    project = advance(client, slug)

    assert project.phase == "plan"
    assert seat.agent_mapping(root) == {"other": "project-other"}
    assert [r["operation"] for r in audit_records(root)[len(before):]] == ["advance_project"]
