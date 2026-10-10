"""Every request to the daemon carries a bearer token that names who acts.

A request with no token, or one the daemon does not know, is refused with a
401 that says where to get a personal token. A known token hands every route
the principal it acts as: a user under the role their row holds, or the
built-in agent, the pipeline agent the daemon runs. The agent acts over the
API like a user and every mutation it makes is audited under its own name.
Minting a token for a fixed identity into the older tokens file is gone; how
that file is moved into the store is covered in test_daemon_migration.py.
"""

import json

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import auth, routes
from specflo.daemon.app import create_app
from specflo.service import wire

from conftest import role_token


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


def _bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


# --- requests ---------------------------------------------------------------


@pytest.fixture
def client(root):
    return TestClient(create_app(root))


def test_a_request_without_a_token_gets_401(client):
    response = client.get("/whoami")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["detail"] == auth.TOKEN_REQUIRED


def test_a_request_with_an_unknown_token_gets_401(root, client):
    role_token(root, "requester")

    refused = client.get("/whoami", headers=_bearer("not-a-token"))
    assert refused.status_code == 401
    assert refused.json()["detail"] == auth.TOKEN_REQUIRED
    assert client.get("/whoami", headers={"Authorization": "Basic abc"}).status_code == 401


def test_the_daemon_knows_exactly_three_roles():
    assert auth.IDENTITIES == ("requester", "developer", "agent")


def test_a_valid_token_attaches_its_identity_to_the_request(root, client):
    assert "agent" in auth.IDENTITIES
    for identity in auth.IDENTITIES:
        secret = role_token(root, identity)

        response = client.get("/whoami", headers=_bearer(secret))

        assert response.status_code == 200
        assert response.json() == {"identity": identity, "user": identity, "role": identity}


def test_the_health_endpoint_needs_no_token(client):
    assert client.get(daemon.HEALTH_PATH).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_the_generated_docs_and_schema_are_not_served(client, root, path):
    # They would hand every API path to a browser without a token; the
    # token guard is the contract, so the daemon does not generate them.
    assert client.get(path).status_code == 404
    secret = role_token(root, "developer")
    assert client.get(path, headers=_bearer(secret)).status_code == 404


def test_a_mutation_by_the_agent_is_audited_as_agent(root, client):
    secret = role_token(root, "agent")
    headers = _bearer(secret)

    def post(operation, **body):
        response = client.post(wire.route_path(operation), json=body, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["result"]

    post("create_project", name="Audited")
    post("start_brainstorm", slug="audited")
    post("add_decision", slug="audited", text="The agent decided")

    records = [
        json.loads(line)
        for line in (root / routes.AUDIT_FILENAME).read_text().splitlines()
    ]
    decision = [r for r in records if r["operation"] == "add_decision"]
    assert len(decision) == 1
    assert decision[0]["user"] == "agent"
    assert decision[0]["project"] == "audited"
    assert {r["user"] for r in records} == {"agent"}
