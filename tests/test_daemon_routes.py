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
from specflo.service.local import LocalProjectService


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
    assert created and path == Path(daemon.PROJECTS_DIRNAME) / "my-thing" / "brainstorm.md"
    assert (root / path).is_file()
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


def test_an_argument_of_the_wrong_type_is_a_422_not_a_500(client):
    call(client, "create_project", name="Thing")

    for operation, body, expected in (
        ("add_decision", {"slug": 5, "text": "x"}, "'slug' must be a string"),
        ("add_decision", {"slug": "thing", "text": ["x"]}, "'text' must be a string"),
        ("add_milestone", {"slug": "thing", "text": "m", "exit_items": "x"}, "'exit_items' must be a list"),
        ("import_project", {"slug": "other", "files": "nope"}, "'files' must be an object"),
    ):
        response = client.post(wire.route_path(operation), json=body)
        assert response.status_code == 422, (operation, response.text)
        assert expected in response.json()["detail"], (operation, response.text)


def test_a_slug_that_is_not_one_is_refused_before_anything_is_written(root, client, tmp_path):
    # The daemon root holds every hosted project; a slug is the one thing a
    # request contributes to a path under it, so only a slug that slugify
    # could have made is accepted, for every operation that names one.
    outside_before = sorted(p.name for p in tmp_path.iterdir())

    for slug in ("../escape", "a/b", ".hidden", "Upper", "two--hyphens", "-lead", ""):
        response = client.post(
            wire.route_path("import_project"), json={"slug": slug, "files": {"project.md": "x"}}
        )
        assert response.status_code == 400, (slug, response.text)
        assert "slug" in response.json()["detail"], (slug, response.text)
        for operation, extra in (
            ("load_project", {}),
            ("start_brainstorm", {}),
            ("add_decision", {"text": "x"}),
            ("show_document", {"name": "brainstorm"}),
        ):
            response = client.post(wire.route_path(operation), json={"slug": slug, **extra})
            assert response.status_code == 400, (operation, slug, response.text)
            assert "slug" in response.json()["detail"], (operation, slug, response.text)

    assert sorted(p.name for p in tmp_path.iterdir()) == outside_before
    assert not list((root / daemon.PROJECTS_DIRNAME).iterdir())
    assert not list(root.glob(".*.importing"))


def test_close_round_takes_the_report_text_and_never_a_path(root, client):
    # The client reads its own --file; only the text travels. A path in the
    # request would name a file on the daemon host, which is nobody's to read.
    call(client, "create_project", name="Thing")
    call(client, "start_round", slug="thing")
    assert "report" not in wire.OPERATIONS["close_round"].hints
    assert wire.OPERATIONS["close_round"].hints["report_text"] == (str | None)

    by_path = client.post(
        wire.route_path("close_round"),
        json={"slug": "thing", "verdict": "ready-to-merge", "report": "/etc/hostname"},
    )
    assert by_path.status_code == 422 and "report" in by_path.text

    text = "# Round 1\n\n## Findings\n\n- none\n"
    path = call(client, "close_round", slug="thing", verdict="ready-to-merge", report_text=text)

    assert path == Path(daemon.PROJECTS_DIRNAME) / "thing" / "review-1.md"
    assert (root / path).read_text().endswith(text)


def test_a_bad_element_is_refused_and_a_failure_inside_is_a_500_without_a_traceback(
    client, root, monkeypatch
):
    call(client, "create_project", name="Thing")

    # A file name with a control character never reaches the filesystem.
    nul = client.post(
        wire.route_path("import_project"),
        json={"slug": "other", "files": {"project.md": "x", "bad\x00name.md": "y"}},
    )
    assert nul.status_code == 400, nul.text
    assert "file name" in nul.json()["detail"]
    assert not (root / daemon.PROJECTS_DIRNAME / "other").exists()

    # The elements of a list or object are typed too, not only the container.
    for operation, body, expected in (
        ("import_project", {"slug": "other", "files": {"project.md": 1}},
         "'files' must be an object of strings"),
        ("add_task", {"slug": "thing", "text": "t", "acceptance": "a", "verify": "v",
                      "implements": [1, 2]},
         "'implements' must be a list of strings"),
        ("add_milestone", {"slug": "thing", "text": "m", "exit_items": ["ok", None]},
         "'exit_items' must be a list of strings"),
    ):
        response = client.post(wire.route_path(operation), json=body)
        assert response.status_code == 422, (operation, response.text)
        assert expected in response.json()["detail"], (operation, response.text)

    # Whatever else fails inside the service answers as JSON naming the
    # operation and the kind of failure; the traceback stays in the log.
    def broken(self, slug):
        raise RuntimeError("/srv/daemon/projects/thing/project.md is unreadable")

    monkeypatch.setattr(LocalProjectService, "load_project", broken)
    response = client.post(wire.route_path("load_project"), json={"slug": "thing"})

    assert response.status_code == 500, response.text
    detail = response.json()["detail"]
    assert "load_project" in detail and "RuntimeError" in detail
    assert "Traceback" not in response.text and "/srv/daemon" not in response.text
