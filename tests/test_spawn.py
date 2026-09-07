"""Spawn: a full-path work item gets exactly one specflo project, cross-linked both ways.

The project is created on the daemon that holds the work item, as a hosted
project: its front matter records the work item (and the piece the item
targets), and the work item records the project's slug. A second spawn is
refused naming that project, and an item on any other dev path is refused.
The CLI records the new project as hosted and makes it active, as ``new
--remote`` does.
"""

import dataclasses
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import config, daemon, projects
from specflo.cli import app
from specflo.agent.statefiles import ENV_STATE_DIR
from specflo.daemon import auth, routes, seat
from specflo.daemon import store as store_module
from specflo.daemon.app import create_app
from specflo.daemon.products import Products
from specflo.daemon.workitems import WORK_ITEMS_PATH, RemoteWorkItems, Spawned, WorkItems
from specflo.errors import SpecfloError
from specflo.service.local import LocalProjectService

runner = CliRunner()


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def token(root):
    return auth.mint_token(root, "developer")


@pytest.fixture
def client(root, token):
    client = TestClient(create_app(root))
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def seed(root):
    """Product ``thing`` with piece ``web``; items 1 (full, web), 2 (one-prompt), 3 (full)."""
    with store_module.open_store(root) as store:
        Products(store).add("Thing", slug="thing", today="2026-09-06")
        Products(store).add_piece("thing", "web")
        items = WorkItems(store)
        items.add("thing", "Fix the login", piece="web", today="2026-09-06")
        items.add("thing", "Tidy the readme", dev_path="one-prompt", today="2026-09-06")
        items.add("thing", "Offline mode", today="2026-09-06")


def service(root):
    return LocalProjectService(root, config.load_config(root))


def front_matter(root, slug):
    text = (root / daemon.PROJECTS_DIRNAME / slug / "project.md").read_text()
    return projects._parse_frontmatter(text)


def audit_records(root):
    path = root / routes.AUDIT_FILENAME
    if not path.is_file():
        return []
    return [
        (r["identity"], r["project"], r["operation"], r["id"])
        for r in (json.loads(line) for line in path.read_text().splitlines())
    ]


# --- the project record --------------------------------------------------------


def test_a_project_records_its_work_item_and_piece_in_front_matter(tmp_path):
    config.init_config(tmp_path)
    cfg = config.load_config(tmp_path)

    linked = projects.create_project(tmp_path, cfg, "Fix the login", work_item=3, piece="web")
    assert linked.work_item == 3 and linked.piece == "web"
    loaded = projects.load_project(tmp_path, cfg, "fix-the-login")
    assert loaded == linked
    text = (linked.path / "project.md").read_text()
    assert "work_item: 3\n" in text and "piece: web\n" in text

    plain = projects.create_project(tmp_path, cfg, "Plain")
    assert plain.work_item is None and plain.piece == ""
    text = (plain.path / "project.md").read_text()
    assert "work_item" not in text and "piece" not in text
    assert projects.load_project(tmp_path, cfg, "plain") == plain


# --- the spawn verb in-process -------------------------------------------------


def test_spawn_creates_one_hosted_project_cross_linked_with_the_item(root):
    seed(root)
    with store_module.open_store(root) as store:
        spawned = WorkItems(store).spawn(1, service(root))

    assert isinstance(spawned, Spawned)
    assert spawned.project.slug == "fix-the-login"
    assert spawned.project.name == "Fix the login"
    assert spawned.project.summary == "Fix the login"
    assert spawned.project.phase == "brainstorm"
    assert spawned.project.work_item == 1 and spawned.project.piece == "web"
    assert spawned.item.id == 1 and spawned.item.project == "fix-the-login"
    assert spawned.brainstorm.name == "brainstorm.md"

    hosted = root / daemon.PROJECTS_DIRNAME / "fix-the-login"
    assert front_matter(root, "fix-the-login")["work_item"] == 1
    assert front_matter(root, "fix-the-login")["piece"] == "web"
    assert (hosted / "brainstorm.md").is_file() and (hosted / "checkpoint.md").is_file()
    with store_module.open_store(root) as store:
        assert WorkItems(store).show(1).project == "fix-the-login"
    assert service(root).load_project("fix-the-login") == spawned.project


def test_spawn_takes_a_project_name_and_leaves_piece_out_when_the_item_has_none(root):
    seed(root)
    with store_module.open_store(root) as store:
        spawned = WorkItems(store).spawn(3, service(root), name="Offline Mode v2")

    assert spawned.project.slug == "offline-mode-v2"
    assert spawned.project.name == "Offline Mode v2"
    assert spawned.project.summary == "Offline mode"
    assert spawned.project.work_item == 3 and spawned.project.piece == ""
    assert "piece" not in front_matter(root, "offline-mode-v2")
    assert spawned.item.project == "offline-mode-v2"


def test_spawn_is_refused_a_second_time_and_for_other_dev_paths(root):
    seed(root)
    with store_module.open_store(root) as store:
        items = WorkItems(store)
        items.spawn(1, service(root))

        with pytest.raises(SpecfloError, match="Work item 1 already spawned project 'fix-the-login'"):
            items.spawn(1, service(root))
        with pytest.raises(SpecfloError, match="Work item 2 has dev path 'one-prompt'; only a 'full'"):
            items.spawn(2, service(root))
        with pytest.raises(SpecfloError, match="No work item 9"):
            items.spawn(9, service(root))
        assert items.show(2).project is None
    assert [p.slug for p in service(root).list_projects()] == ["fix-the-login"]


def test_a_slug_the_daemon_already_holds_leaves_the_item_unspawned(root):
    seed(root)
    service(root).create_project("Fix the login")
    with store_module.open_store(root) as store:
        items = WorkItems(store)
        with pytest.raises(SpecfloError, match="already exists"):
            items.spawn(1, service(root))
        assert items.show(1).project is None
        assert items.spawn(1, service(root), name="Fix the login again").item.project == (
            "fix-the-login-again"
        )


# --- the route and the remote client -------------------------------------------


def test_spawn_route_round_trips_and_records_the_actor(client, root):
    seed(root)

    spawned = client.post(f"{WORK_ITEMS_PATH}/1/spawn")
    assert spawned.status_code == 200, spawned.text
    result = spawned.json()["result"]
    assert result["item"]["id"] == 1 and result["item"]["project"] == "fix-the-login"
    assert result["project"]["slug"] == "fix-the-login" and result["project"]["work_item"] == 1
    assert result["brainstorm"] == f"{daemon.PROJECTS_DIRNAME}/fix-the-login/brainstorm.md"
    assert result["project"]["path"] == f"{daemon.PROJECTS_DIRNAME}/fix-the-login"
    assert str(root) not in spawned.text

    named = client.post(f"{WORK_ITEMS_PATH}/3/spawn", json={"name": "Offline Mode v2"})
    assert named.status_code == 200, named.text
    assert named.json()["result"]["project"]["slug"] == "offline-mode-v2"

    again = client.post(f"{WORK_ITEMS_PATH}/1/spawn")
    assert again.status_code == 400 and "already spawned project 'fix-the-login'" in again.json()["detail"]
    other_path = client.post(f"{WORK_ITEMS_PATH}/2/spawn")
    assert other_path.status_code == 400 and "dev path 'one-prompt'" in other_path.json()["detail"]
    extra = client.post(f"{WORK_ITEMS_PATH}/2/spawn", json={"slug": "x"})
    assert extra.status_code == 422 and "slug" in extra.json()["detail"]
    assert TestClient(create_app(root)).post(f"{WORK_ITEMS_PATH}/1/spawn").status_code == 401

    assert audit_records(root) == [
        ("developer", "fix-the-login", "workitem_spawn", "1"),
        ("developer", "offline-mode-v2", "workitem_spawn", "3"),
    ]
    shown = client.get(f"{WORK_ITEMS_PATH}/1").json()["result"]
    assert shown["project"] == "fix-the-login"


def test_remote_spawn_mirrors_the_in_process_verb(root, token):
    seed(root)
    remote = RemoteWorkItems("http://testserver", token, client=TestClient(create_app(root)))

    spawned = remote.spawn(1)
    assert isinstance(spawned, Spawned)
    assert spawned.item.project == "fix-the-login"
    held = service(root).load_project("fix-the-login")
    assert spawned.project == dataclasses.replace(held, path=Path(daemon.PROJECTS_DIRNAME) / "fix-the-login")
    assert spawned.brainstorm == Path(daemon.PROJECTS_DIRNAME) / "fix-the-login" / "brainstorm.md"
    assert remote.spawn(3, name="Offline Mode v2").project.slug == "offline-mode-v2"
    with pytest.raises(SpecfloError, match="already spawned"):
        remote.spawn(1)


# --- the CLI verb ------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path, monkeypatch, live_daemon):
    """A checkout with the live daemon as ``home``, whose store holds the seed."""
    seed(live_daemon["root"])
    root = tmp_path / "checkout"
    root.mkdir()
    config.init_config(root)
    monkeypatch.chdir(root)
    done = runner.invoke(
        app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]]
    )
    assert done.exit_code == 0, done.output
    return root


def test_workitem_spawn_creates_the_hosted_project_and_makes_it_active(checkout, live_daemon):
    result = runner.invoke(app, ["workitem", "spawn", "1"])
    assert result.exit_code == 0, result.output
    assert result.output == (
        "Spawned project 'fix-the-login' from work item 1 on remote 'home' (now active)."
        " Phase: brainstorm.\n"
        "Scaffolded fix-the-login/brainstorm (ready to work).\n"
    )

    assert config.hosted_projects(checkout) == {"fix-the-login": "home"}
    assert config.load_config(checkout).active_project == "fix-the-login"
    assert not (checkout / "docs" / "projects" / "fix-the-login").exists()
    hosted = live_daemon["root"] / daemon.PROJECTS_DIRNAME / "fix-the-login"
    assert (hosted / "project.md").is_file() and (hosted / "brainstorm.md").is_file()

    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0 and "Project: Fix the login (fix-the-login)" in status.output
    shown = runner.invoke(app, ["doc", "show", "project"])
    assert "work_item: 1\n" in shown.output and "piece: web\n" in shown.output
    listing = runner.invoke(app, ["list"]).output
    assert "* fix-the-login  (brainstorm)  [hosted: home]" in listing.splitlines()

    item = runner.invoke(app, ["workitem", "show", "1"])
    assert "Project:   fix-the-login\n" in item.output
    assert json.loads(runner.invoke(app, ["workitem", "show", "1", "--json"]).output)["project"] == (
        "fix-the-login"
    )


def test_workitem_spawn_refuses_what_it_cannot_do(checkout, live_daemon):
    assert runner.invoke(app, ["workitem", "spawn", "1"]).exit_code == 0

    again = runner.invoke(app, ["workitem", "spawn", "1"])
    assert again.exit_code == 1
    assert "Work item 1 already spawned project 'fix-the-login'" in again.stderr
    other_path = runner.invoke(app, ["workitem", "spawn", "2"])
    assert other_path.exit_code == 1 and "dev path 'one-prompt'" in other_path.stderr
    unknown = runner.invoke(app, ["workitem", "spawn", "9"])
    assert unknown.exit_code == 1 and "No work item 9" in unknown.stderr

    # A slug this checkout already holds locally is refused before anything
    # is spawned, so the item stays unspawned.
    assert runner.invoke(app, ["new", "Offline mode"]).exit_code == 0
    clash = runner.invoke(app, ["workitem", "spawn", "3"])
    assert clash.exit_code == 1 and "already exists in this checkout" in clash.stderr
    assert "Project:   -\n" in runner.invoke(app, ["workitem", "show", "3"]).output
    assert not (live_daemon["root"] / daemon.PROJECTS_DIRNAME / "offline-mode").exists()

    named = runner.invoke(app, ["workitem", "spawn", "3", "--name", "Offline mode v2"])
    assert named.exit_code == 0, named.output
    assert config.hosted_projects(checkout) == {"fix-the-login": "home", "offline-mode-v2": "home"}
    assert config.load_config(checkout).active_project == "offline-mode-v2"


def test_a_spawn_that_fails_part_way_leaves_nothing_behind_and_the_retry_succeeds(root, monkeypatch):
    seed(root)
    svc = service(root)
    real = svc.write_checkpoint
    monkeypatch.setattr(svc, "write_checkpoint", lambda slug: (_ for _ in ()).throw(OSError("disk full")))

    with store_module.open_store(root) as store:
        items = WorkItems(store)
        with pytest.raises(OSError, match="disk full"):
            items.spawn(1, svc)
        assert items.show(1).project is None
        assert not (root / daemon.PROJECTS_DIRNAME / "fix-the-login").exists()
        assert svc.list_projects() == []

        monkeypatch.setattr(svc, "write_checkpoint", real)
        spawned = items.spawn(1, svc)

        assert spawned.item.project == "fix-the-login"
        assert items.show(1).project == "fix-the-login"
    assert [p.slug for p in svc.list_projects()] == ["fix-the-login"]
    assert (root / daemon.PROJECTS_DIRNAME / "fix-the-login" / "checkpoint.md").is_file()


# --- start project: spawn, scaffold, start, as one operation ------------------

STUB_PI = Path(__file__).parent / "agent" / "stub_pi.py"


def _agent_cli(*args):
    return subprocess.run(
        [sys.executable, "-c", "import sys; from specflo.cli import main; sys.exit(main())", "agent", *args],
        capture_output=True, text=True, env=dict(os.environ), timeout=30,
    )


@pytest.fixture
def rpc_rig(root, tmp_path, monkeypatch):
    """An isolated agent state dir, the rpc transport on the root, and a stub pi."""
    monkeypatch.setenv(ENV_STATE_DIR, str(tmp_path / "state"))
    for key in ("SPECFLO_AGENT_SERVE", "SPECFLO_AGENT_NAME", "SPECFLO_AGENT_MANAGED"):
        monkeypatch.delenv(key, raising=False)
    config.write_value(root, config.field_for("agent_transport"), "rpc")
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"reply": "ok"}), encoding="utf-8")
    yield {"pi_cmd": f"{sys.executable} {STUB_PI} {scenario}"}
    for name in seat.agent_mapping(root).values():
        _agent_cli("stop", name)


def test_start_project_spawns_scaffolds_and_starts_the_agent(root, rpc_rig):
    seed(root)

    started = seat.start_project(root, 1, "requester", url="http://127.0.0.1:8741", pi_cmd=rpc_rig["pi_cmd"])

    slug = started.spawned.project.slug
    assert slug == "fix-the-login" and started.spawned.item.project == slug
    assert started.seat == seat.seat_dir(root, slug) and config.config_path(started.seat).is_file()
    remote = config.load_remote(started.seat, seat.REMOTE_NAME)
    assert remote.url == "http://127.0.0.1:8741" and auth.identity_for(root, remote.token) == "agent"
    assert started.agent == seat.agent_name(slug) and seat.agent_mapping(root) == {slug: started.agent}
    probe = json.loads(_agent_cli("status", started.agent, "--json").stdout)
    assert probe["alive"] is True and probe["transport"] == "rpc"
    assert audit_records(root) == [
        ("requester", slug, "workitem_spawn", "1"),
        ("requester", slug, "start_project", "1"),
    ]
    with store_module.open_store(root) as store:
        assert WorkItems(store).show(1).project == slug


def test_a_second_start_is_refused_naming_the_project_and_changes_nothing(root, rpc_rig):
    seed(root)
    seat.start_project(root, 1, "requester", pi_cmd=rpc_rig["pi_cmd"])
    before = audit_records(root)
    mapping = seat.agent_mapping(root)

    with pytest.raises(SpecfloError, match="already spawned project 'fix-the-login'"):
        seat.start_project(root, 1, "requester", pi_cmd=rpc_rig["pi_cmd"])

    assert audit_records(root) == before and seat.agent_mapping(root) == mapping
    assert [p.slug for p in service(root).list_projects()] == ["fix-the-login"]


def test_a_failed_agent_start_leaves_the_spawned_project_and_reports_it(root, rpc_rig, monkeypatch):
    seed(root)
    monkeypatch.setenv("SPECFLO_AGENT_START_TIMEOUT", "1")

    with pytest.raises(seat.AgentStartError):
        seat.start_project(root, 1, "requester", pi_cmd=f"{sys.executable} -c 'import sys; sys.exit(3)'")

    assert [p.slug for p in service(root).list_projects()] == ["fix-the-login"]
    with store_module.open_store(root) as store:
        assert WorkItems(store).show(1).project == "fix-the-login"
    assert config.config_path(seat.seat_dir(root, "fix-the-login")).is_file()
    assert seat.agent_mapping(root) == {}
    assert audit_records(root) == [("requester", "fix-the-login", "workitem_spawn", "1")]


def test_start_project_refuses_what_spawn_refuses_before_any_seat(root, rpc_rig):
    seed(root)

    with pytest.raises(SpecfloError, match="dev path 'one-prompt'"):
        seat.start_project(root, 2, "requester", pi_cmd=rpc_rig["pi_cmd"])

    assert not (root / seat.SEATS_DIRNAME).exists()
    assert service(root).list_projects() == [] and audit_records(root) == []


def test_one_function_performs_the_three_steps_and_the_route_only_calls_it():
    body = inspect.getsource(seat.start_project)
    steps = [body.index(".spawn("), body.index("scaffold("), body.index("start_agent(")]
    assert steps == sorted(steps)
    route = inspect.getsource(routes.workitem_start_project)
    assert "start_project(" in route
    for step in (".spawn(", "scaffold(", "start_agent(", "mint_token("):
        assert step not in route, step


def test_the_start_project_route_calls_the_operation_and_names_the_seat(client, root, rpc_rig, monkeypatch):
    seed(root)
    monkeypatch.setattr(seat, "DEFAULT_PI_CMD", rpc_rig["pi_cmd"])

    response = client.post(f"{WORK_ITEMS_PATH}/1/start-project")

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["spawned"]["project"]["slug"] == "fix-the-login"
    assert result["seat"] == f"{seat.SEATS_DIRNAME}/fix-the-login"
    assert result["agent"] == seat.agent_name("fix-the-login")
    assert str(root) not in response.text
    assert seat.agent_mapping(root) == {"fix-the-login": result["agent"]}

    again = client.post(f"{WORK_ITEMS_PATH}/1/start-project")
    assert again.status_code == 400 and "already spawned project 'fix-the-login'" in again.json()["detail"]
    assert client.post(f"{WORK_ITEMS_PATH}/2/start-project").status_code == 400
    assert TestClient(create_app(root)).post(f"{WORK_ITEMS_PATH}/3/start-project").status_code == 401


def test_the_route_reports_a_failed_agent_start_as_a_bad_gateway(client, root, rpc_rig, monkeypatch):
    seed(root)
    monkeypatch.setenv("SPECFLO_AGENT_START_TIMEOUT", "1")
    monkeypatch.setattr(seat, "DEFAULT_PI_CMD", f"{sys.executable} -c 'import sys; sys.exit(3)'")

    response = client.post(f"{WORK_ITEMS_PATH}/1/start-project")

    assert response.status_code == 502
    assert "fix-the-login" in response.json()["detail"]
    assert str(root) not in response.text
    assert [p.slug for p in service(root).list_projects()] == ["fix-the-login"]


def test_the_route_hands_the_daemons_own_url_to_the_seat(root, rpc_rig, monkeypatch):
    seed(root)
    monkeypatch.setattr(seat, "DEFAULT_PI_CMD", rpc_rig["pi_cmd"])
    application = create_app(root, url="http://daemon.local:9000")
    client = TestClient(application)
    client.headers["Authorization"] = f"Bearer {auth.mint_token(root, 'developer')}"

    assert client.post(f"{WORK_ITEMS_PATH}/1/start-project").status_code == 200

    remote = config.load_remote(seat.seat_dir(root, "fix-the-login"), seat.REMOTE_NAME)
    assert remote.url == "http://daemon.local:9000"
