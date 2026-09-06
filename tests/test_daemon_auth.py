"""Bearer tokens bound to the two identities the daemon knows.

Every request to the daemon carries a bearer token minted on the serve side
for one of two identities, requester or developer. The daemon stores only
the token's hash, refuses a request with no or an unknown token, and hands
every route the identity the token is bound to.
"""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import daemon
from specflo.cli import app
from specflo.daemon import auth
from specflo.daemon.app import create_app
from specflo.errors import SpecfloError

runner = CliRunner()


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


def _stored(root):
    return json.loads((root / auth.TOKENS_FILENAME).read_text())["tokens"]


def _bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


# --- minting ----------------------------------------------------------------


def test_mint_stores_only_the_hash_bound_to_the_identity(root):
    secret = auth.mint_token(root, "requester", today="2026-09-06")

    assert _stored(root) == [
        {
            "identity": "requester",
            "hash": hashlib.sha256(secret.encode()).hexdigest(),
            "created": "2026-09-06",
        }
    ]
    assert secret not in (root / auth.TOKENS_FILENAME).read_text()


def test_mint_refuses_an_identity_outside_the_two(root):
    with pytest.raises(SpecfloError, match="requester, developer"):
        auth.mint_token(root, "admin")

    assert not (root / auth.TOKENS_FILENAME).exists()


def test_each_mint_is_a_fresh_secret_and_every_token_stays_valid(root):
    first = auth.mint_token(root, "developer")
    second = auth.mint_token(root, "developer")

    assert first != second
    assert auth.identity_for(root, first) == "developer"
    assert auth.identity_for(root, second) == "developer"
    assert auth.identity_for(root, "not-a-token") is None
    assert auth.identity_for(root, "") is None


def test_identity_for_answers_none_before_any_token_exists(root):
    assert auth.identity_for(root, "anything") is None


# --- the command ------------------------------------------------------------


def test_token_add_prints_the_secret_once_and_starts_no_server(root, monkeypatch):
    import uvicorn

    monkeypatch.setattr(
        uvicorn, "run", lambda *args, **kwargs: pytest.fail("token add started a server")
    )

    result = runner.invoke(app, ["serve", "--root", str(root), "token", "add", "requester"])

    assert result.exit_code == 0, result.output
    secret = result.stdout.strip()
    assert "\n" not in secret and len(secret) >= 32
    assert auth.identity_for(root, secret) == "requester"
    assert secret not in (root / auth.TOKENS_FILENAME).read_text()
    assert "shown once" in result.stderr


def test_token_add_prepares_a_root_that_does_not_exist_yet(tmp_path):
    root = tmp_path / "fresh"

    result = runner.invoke(app, ["serve", "--root", str(root), "token", "add", "developer"])

    assert result.exit_code == 0, result.output
    assert (root / daemon.PROJECTS_DIRNAME).is_dir()
    assert auth.identity_for(root, result.stdout.strip()) == "developer"


def test_token_add_refuses_an_unknown_identity(root):
    result = runner.invoke(app, ["serve", "--root", str(root), "token", "add", "admin"])

    assert result.exit_code == 1
    assert "requester, developer" in result.stderr
    assert result.stdout == ""


# --- requests ---------------------------------------------------------------


@pytest.fixture
def client(root):
    return TestClient(create_app(root))


def test_a_request_without_a_token_gets_401(client):
    response = client.get("/whoami")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_request_with_an_unknown_token_gets_401(root, client):
    auth.mint_token(root, "requester")

    assert client.get("/whoami", headers=_bearer("not-a-token")).status_code == 401
    assert client.get("/whoami", headers={"Authorization": "Basic abc"}).status_code == 401


def test_a_valid_token_attaches_its_identity_to_the_request(root, client):
    for identity in auth.IDENTITIES:
        secret = auth.mint_token(root, identity)

        response = client.get("/whoami", headers=_bearer(secret))

        assert response.status_code == 200
        assert response.json() == {"identity": identity}


def test_the_health_endpoint_needs_no_token(client):
    assert client.get(daemon.HEALTH_PATH).status_code == 200
