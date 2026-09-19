"""A daemon started on a pool configuration with faults: projects on, the pool off.

The daemon checks the pool directory when it starts, with the check the
``pool validate`` verb runs. Faults there are the pool's matter alone: the
daemon starts, serves its projects as ever, and answers every pool route with
every fault, in the words the verb prints, so an orchestrator reads what the
admin has to correct. The faults are kept on the application for the pages
that show them. A root with no pool directory has no faults and no pool, and
says so; a directory that stands is served.

The directories are the validation verb's own, the daemon and the checkout
the request verb's.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.cli import app
from specflo.daemon import auth, pool_routes
from specflo.daemon.app import create_app
from specflo.pool import cli_admin
from specflo.pool import config as pool_config
from specflo.service import wire
from specflo.service.pool_remote import HELD_PATH, LEASES_PATH, STATUS_PATH, release_path

from . import test_lease_request
from .test_cli_validate import _write, _write_three_faults
from .test_lease_request import checkout, pool_daemon, runner  # noqa: F401  (fixtures)

NO_POOL = "No pool is configured on this daemon."


def client_on(root) -> TestClient:
    """The daemon on *root*, asked as the developer."""
    client = TestClient(create_app(root))
    client.headers["Authorization"] = f"Bearer {auth.mint_token(root, 'developer')}"
    return client


def faults_of(root) -> list[str]:
    _, faults = pool_config.load_pool_config(cli_admin.pool_dir(root))
    return [str(fault) for fault in faults]


def ask_every_pool_route(client):
    """One request to each route of the pool's router, as (what was asked, the answer)."""
    for route in pool_routes.router.routes:
        path = route.path.replace("{lease_id}", "lease-1")
        for method in sorted(route.methods):
            kwargs = {} if method == "GET" else {"json": {}}
            yield f"{method} {path}", client.request(method, path, **kwargs)


@pytest.fixture
def faulty_root(tmp_path):
    root = _write_three_faults(tmp_path / "daemon")
    assert len(faults_of(root)) == 3
    return root


# -- a configuration with faults ----------------------------------------------


def test_a_daemon_on_a_faulty_pool_directory_serves_projects_as_ever(faulty_root):
    client = client_on(faulty_root)

    assert client.get(daemon.HEALTH_PATH).status_code == 200
    created = client.post(wire.route_path("create_project"), json={"name": "My Thing"})
    assert created.status_code == 200, created.text
    listed = client.post(wire.route_path("list_projects"), json={})
    assert listed.status_code == 200, listed.text
    assert [project["name"] for project in listed.json()["result"]] == ["My Thing"]


def test_every_pool_route_answers_with_every_fault(faulty_root):
    client = client_on(faulty_root)
    faults = faults_of(faulty_root)

    answers = dict(ask_every_pool_route(client))

    for asked in (
        f"POST {LEASES_PATH}", f"POST {HELD_PATH}",
        f"POST {release_path('lease-1')}", f"GET {STATUS_PATH}",
    ):
        assert asked in answers
    for asked, response in answers.items():
        assert response.status_code == 400, (asked, response.text)
        detail = response.json()["detail"]
        assert isinstance(detail, str), asked
        for fault in faults:
            assert fault in detail, (asked, detail)
        assert NO_POOL not in detail


def test_a_whole_request_is_refused_with_the_faults_like_an_empty_one(faulty_root, tmp_path):
    client = client_on(faulty_root)

    response = client.post(LEASES_PATH, json={"pool": "workers", "cwd": str(tmp_path)})

    assert response.status_code == 400
    for fault in faults_of(faulty_root):
        assert fault in response.json()["detail"]
    assert not (faulty_root / "audit.jsonl").exists()


def test_the_pool_routes_still_need_a_bearer_token(faulty_root):
    client = TestClient(create_app(faulty_root))

    for asked, response in ask_every_pool_route(client):
        assert response.status_code == 401, asked
        assert "coder-b" not in response.text


def test_the_faults_are_kept_on_the_application(faulty_root):
    application = create_app(faulty_root)

    assert application.state.pool is None
    assert [str(fault) for fault in application.state.pool_errors] == faults_of(faulty_root)


def test_the_request_verb_prints_every_fault(monkeypatch, request):
    # The request verb's daemon writes its pool directory before it starts;
    # here that directory is the one with the faults.
    monkeypatch.setattr(
        test_lease_request, "write_pool", lambda rig: _write_three_faults(rig.root)
    )
    served = request.getfixturevalue("pool_daemon")
    request.getfixturevalue("checkout")

    result = runner.invoke(app, ["lease", "request", "workers"])

    assert result.exit_code != 0
    output = " ".join(result.output.split())
    for fault in faults_of(served["root"]):
        assert " ".join(fault.split()) in output, result.output
    assert "Traceback" not in result.output
    assert request.getfixturevalue("pool_rig").pane_names() == []


# -- a configuration that stands, and none ------------------------------------


def test_a_daemon_on_a_valid_pool_directory_serves_the_pool(tmp_path):
    root = _write(tmp_path / "daemon")
    application = create_app(root)
    client = TestClient(application)
    client.headers["Authorization"] = f"Bearer {auth.mint_token(root, 'developer')}"

    response = client.get(STATUS_PATH)

    assert response.status_code == 200, response.text
    assert response.json()["result"] == [
        {"name": "workers", "size": 2, "in_use": 0},
        {"name": "critics", "size": 2, "in_use": 0},
    ]
    assert application.state.pool is not None
    assert list(application.state.pool_errors) == []


def test_a_root_with_no_pool_directory_serves_projects_and_says_no_pool_is_configured(tmp_path):
    root = daemon.prepare_root(tmp_path / "plain")
    application = create_app(root)
    client = TestClient(application)
    client.headers["Authorization"] = f"Bearer {auth.mint_token(root, 'developer')}"

    assert client.post(wire.route_path("list_projects"), json={}).status_code == 200
    for asked, response in ask_every_pool_route(client):
        assert response.status_code == 400, (asked, response.text)
        assert response.json()["detail"] == NO_POOL
    assert application.state.pool is None
    assert list(application.state.pool_errors) == []
