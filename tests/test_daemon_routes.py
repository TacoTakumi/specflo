"""Daemon routes: every ProjectService operation over HTTP, serialized per project.

The daemon runs the local service on its own root behind one route per
operation. A request carries the operation's arguments as a JSON object and
receives the result encoded by the wire layer, so a client rebuilds the same
values the local service returns. Mutations to one project are serialized,
so concurrent adds mint distinct sequential ids and lose nothing.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import auth
from specflo.daemon.app import create_app
from specflo.plan import Task
from specflo.projects import Project
from specflo.service import ProjectService, wire


def operations() -> list[str]:
    return [
        name
        for name, value in vars(ProjectService).items()
        if callable(value) and not name.startswith("_")
    ]


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


def call(client, operation, **kwargs):
    """Invoke one operation over HTTP and decode its result like a client would."""
    response = client.post(wire.route_path(operation), json=kwargs)
    assert response.status_code == 200, response.text
    return wire.decode(response.json()["result"], wire.OPERATIONS[operation].returns)


# --- the wire schema --------------------------------------------------------


def test_wire_names_every_protocol_operation_once():
    assert list(wire.OPERATIONS) == operations()
    paths = [wire.route_path(name) for name in wire.OPERATIONS]
    assert len(set(paths)) == len(paths)
    for name, op in wire.OPERATIONS.items():
        assert op.name == name
        assert op.returns is not None


def test_wire_knows_which_operations_address_one_project():
    assert wire.OPERATIONS["add_decision"].slug_scoped
    assert wire.OPERATIONS["show_document"].slug_scoped
    assert not wire.OPERATIONS["create_project"].slug_scoped
    assert not wire.OPERATIONS["list_projects"].slug_scoped


def test_wire_round_trips_each_return_shape():
    project = Project(
        name="Thing", slug="thing", created="2026-09-06", phase="brainstorm",
        status="active", path=Path("/srv/daemon/projects/thing"),
    )
    task = Task(
        id="x", text="Build", acceptance="works", verify="pytest", implements=["y"],
        depends_on=[], files=None, scope=None, progress="pending", status="active",
    )
    cases = [
        (project, wire.OPERATIONS["load_project"].returns),
        ([project], wire.OPERATIONS["list_projects"].returns),
        ((Path("/srv/daemon/projects/thing/brainstorm.md"), True), wire.OPERATIONS["start_brainstorm"].returns),
        ([task], wire.OPERATIONS["list_tasks"].returns),
        (task, wire.OPERATIONS["start_task"].returns),
        (("x", ["scope"]), wire.OPERATIONS["edit_task"].returns),
        (("fan-out", False), wire.OPERATIONS["set_execution"].returns),
        ({"total": 3}, wire.OPERATIONS["milestone_detail"].returns),
        (None, wire.OPERATIONS["milestone_detail"].returns),
        ("the text", wire.OPERATIONS["show_document"].returns),
        (["issue"], wire.OPERATIONS["validate_artifact"].returns),
        ({"gpu": 2}, wire.OPERATIONS["list_pools"].returns),
        (None, wire.OPERATIONS["complete_artifact"].returns),
        (True, wire.OPERATIONS["has_artifact"].returns),
        (Path("/srv/daemon/projects/thing/review-1.md"), wire.OPERATIONS["close_round"].returns),
    ]
    for value, hint in cases:
        encoded = wire.encode(value)
        assert wire.decode(encoded, hint) == value, hint
    assert wire.encode(project)["path"] == "/srv/daemon/projects/thing"


# --- the routes -------------------------------------------------------------


def _walk(routes):
    """Every route, descending into routers the app mounts as one entry."""
    for route in routes:
        yield route
        nested = getattr(route, "original_router", route)
        yield from _walk(getattr(nested, "routes", []) if nested is not route else [])


def test_every_operation_has_a_post_route(root):
    posted = {
        route.path
        for route in _walk(create_app(root).routes)
        if "POST" in (getattr(route, "methods", None) or set())
    }
    assert posted >= {wire.route_path(name) for name in operations()}


def test_routes_refuse_a_request_without_a_token(root):
    client = TestClient(create_app(root))

    assert client.post(wire.route_path("list_projects"), json={}).status_code == 401


def test_a_route_runs_the_local_service_on_the_daemon_root(root, client):
    project = call(client, "create_project", name="My Thing", summary="One line")

    assert isinstance(project, Project)
    assert project.slug == "my-thing"
    assert (root / daemon.PROJECTS_DIRNAME / "my-thing" / "project.md").is_file()
    assert call(client, "list_projects") == [project]

    path, created = call(client, "start_brainstorm", slug="my-thing")
    assert created and path == root / daemon.PROJECTS_DIRNAME / "my-thing" / "brainstorm.md"
    decision = call(client, "add_decision", slug="my-thing", text="Use one facade", rationale="why")
    assert decision.text == "Use one facade"
    assert decision.id in call(client, "show_document", slug="my-thing", name="brainstorm")
    assert call(client, "has_artifact", slug="my-thing", name="brainstorm") is True
    assert call(client, "complete_artifact", slug="my-thing", artifact="brainstorm") is None
    assert call(client, "load_project", slug="my-thing").slug == "my-thing"


def test_a_refused_operation_answers_400_with_the_message(client):
    response = client.post(wire.route_path("load_project"), json={"slug": "nope"})

    assert response.status_code == 400
    assert "No project 'nope'" in response.json()["detail"]


def test_a_missing_argument_answers_422_naming_it(client):
    response = client.post(wire.route_path("add_decision"), json={"slug": "x"})

    assert response.status_code == 422
    assert "text" in response.text


def test_an_unknown_argument_answers_422_naming_it(client):
    response = client.post(wire.route_path("list_projects"), json={"bogus": 1})

    assert response.status_code == 422
    assert "bogus" in response.text


# --- per-project write serialization ---------------------------------------


@pytest.fixture
def live(root):
    """A real daemon on a free loopback port, so requests truly run concurrently."""
    import uvicorn

    config = uvicorn.Config(create_app(root), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "the daemon did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_concurrent_decision_adds_mint_distinct_sequential_ids(root, token, live):
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=live, headers=headers, timeout=30) as client:
        assert client.post(wire.route_path("create_project"), json={"name": "Busy"}).status_code == 200
        assert client.post(wire.route_path("start_brainstorm"), json={"slug": "busy"}).status_code == 200

    def add(n: int) -> str:
        with httpx.Client(base_url=live, headers=headers, timeout=30) as client:
            response = client.post(
                wire.route_path("add_decision"), json={"slug": "busy", "text": f"decision {n}"}
            )
            assert response.status_code == 200, response.text
            return response.json()["result"]["id"]

    with ThreadPoolExecutor(max_workers=20) as pool:
        ids = list(pool.map(add, range(20)))

    assert len(set(ids)) == 20
    numbers = sorted(int(identifier.rsplit("-", 1)[1]) for identifier in ids)
    assert numbers == list(range(1, 21))
    document = (root / daemon.PROJECTS_DIRNAME / "busy" / "brainstorm.md").read_text()
    assert all(f"### {identifier} " in document for identifier in ids)
