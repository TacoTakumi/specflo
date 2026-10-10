"""Personal tokens minted for named users on the serve side.

`specflo serve user token add` mints a bearer token for a user and prints
its secret once; the store keeps only the secret's SHA-256 and its last four
characters, so a listing can tell tokens apart without showing either. A
request with the token acts as the token's owner, under the role the owner
holds now: a role change applies at the next call, and a removed user's
tokens stop working at once. The built-in agent may own tokens too, without
a user row. The last-used time is written at most once a minute, so a busy
client does not write on every call. Tokens from the older tokens file keep
working beside the new ones.
"""

import datetime

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import daemon
from specflo.cli import app
from specflo.daemon import auth, routes
from specflo.daemon.app import create_app
from specflo.daemon.store import SqliteStore, open_store
from specflo.errors import SpecfloError
from specflo.service import wire

runner = CliRunner()


@pytest.fixture
def root(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")
    auth.add_user(root, "rob", "developer", None)
    return root


@pytest.fixture
def client(root):
    return TestClient(create_app(root))


@pytest.fixture
def clock(monkeypatch):
    """The routes' clock, held still at a fixed moment; ``move`` sets it forward."""

    class Clock:
        now = 1_800_000_000.0

        def move(self, seconds):
            self.now += seconds

    held = Clock()
    monkeypatch.setattr(routes, "_now", lambda: held.now)
    return held


@pytest.fixture
def touches(monkeypatch):
    """Every last-used write the store makes, as (hash, when)."""
    seen = []
    real = SqliteStore.touch_token

    def counting(self, token_hash, when):
        seen.append((token_hash, when))
        return real(self, token_hash, when)

    monkeypatch.setattr(SqliteStore, "touch_token", counting)
    return seen


def _token(root, *args):
    return runner.invoke(app, ["serve", "--root", str(root), "user", "token", *args])


def _bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


def _stored(root, owner):
    with open_store(root) as store:
        return store.list_tokens(owner)


# --- minting ----------------------------------------------------------------


def test_token_add_prints_an_sfp_secret_once_and_keeps_only_its_hash(root):
    result = _token(root, "add", "rob", "--name", "laptop")

    assert result.exit_code == 0, result.output
    lines = result.stdout.strip().splitlines()
    secret = lines[0]
    assert secret.startswith("sfp_") and len(secret) > 40
    assert "not be shown again" in result.output
    [token] = _stored(root, "rob")
    assert token.name == "laptop" and token.owner == "rob"
    assert token.hash == auth.hash_token(secret)
    assert token.last4 == secret[-4:]
    assert token.last_used is None
    db = (root / daemon.STATE_STORE_FILENAME).read_bytes()
    assert secret.encode() not in db
    assert secret[4:].encode() not in db


def test_mint_user_token_lowercases_the_owner(root):
    secret = auth.mint_user_token(root, "Rob", "ci", today="2026-10-10")

    [token] = _stored(root, "rob")
    assert token.created == "2026-10-10"
    assert token.hash == auth.hash_token(secret)


def test_a_token_for_an_unknown_user_is_refused(root):
    with pytest.raises(SpecfloError, match="No user named 'nobody'"):
        auth.mint_user_token(root, "nobody", "laptop")

    result = _token(root, "add", "nobody", "--name", "laptop")

    assert result.exit_code == 1
    assert "nobody" in result.output
    assert _stored(root, "nobody") == []


def test_a_duplicate_token_name_for_one_owner_is_refused(root):
    auth.mint_user_token(root, "rob", "laptop")

    with pytest.raises(SpecfloError, match="rob already has a token named 'laptop'"):
        auth.mint_user_token(root, "rob", "laptop")

    result = _token(root, "add", "rob", "--name", "laptop")
    assert result.exit_code == 1
    assert "already has a token named 'laptop'" in result.output
    assert len(_stored(root, "rob")) == 1


def test_two_owners_may_each_have_a_token_of_one_name(root):
    auth.add_user(root, "ann", "requester", None)

    auth.mint_user_token(root, "rob", "laptop")
    auth.mint_user_token(root, "ann", "laptop")

    assert [t.owner for t in _stored(root, None)] == ["ann", "rob"]


def test_the_agent_may_own_tokens_without_a_user_row(root, client):
    secret = auth.mint_user_token(root, "agent", "pool")

    assert auth.principal_for(root, secret) == ("agent", "agent")
    response = client.get(routes.WHOAMI_PATH, headers=_bearer(secret))
    assert response.json() == {"identity": "agent", "user": "agent", "role": "agent"}


# --- listing ----------------------------------------------------------------


def test_token_list_shows_name_created_last_used_and_last4_never_the_secret(root):
    first = auth.mint_user_token(root, "rob", "laptop", today="2026-10-01")
    second = auth.mint_user_token(root, "rob", "ci", today="2026-10-02")
    auth.principal_for(root, second, now=1_800_000_000.0)

    result = _token(root, "list", "rob")

    assert result.exit_code == 0, result.output
    lines = result.stdout.strip().splitlines()
    assert lines[0].split() == ["NAME", "CREATED", "LAST", "USED", "LAST4"]
    ci, laptop = (line.split() for line in lines[1:])
    used = datetime.datetime.fromtimestamp(1_800_000_000, datetime.timezone.utc)
    assert ci == ["ci", "2026-10-02", used.isoformat(timespec="seconds"), second[-4:]]
    assert laptop == ["laptop", "2026-10-01", "never", first[-4:]]
    for token in _stored(root, "rob"):
        assert token.hash not in result.output
    assert first not in result.output and second not in result.output


def test_token_list_of_a_user_without_tokens_says_so(root):
    result = _token(root, "list", "rob")

    assert result.exit_code == 0
    assert "rob has no tokens" in result.output


def test_token_list_of_an_unknown_user_is_refused(root):
    result = _token(root, "list", "nobody")

    assert result.exit_code == 1
    assert "No user named 'nobody'" in result.output


# --- revoking ---------------------------------------------------------------


def test_a_revoked_token_gets_401(root, client):
    secret = auth.mint_user_token(root, "rob", "laptop")
    assert client.get(routes.WHOAMI_PATH, headers=_bearer(secret)).status_code == 200

    result = _token(root, "revoke", "rob", "laptop")

    assert result.exit_code == 0, result.output
    assert "laptop" in result.output
    response = client.get(routes.WHOAMI_PATH, headers=_bearer(secret))
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert _stored(root, "rob") == []


def test_revoking_an_unknown_token_is_refused(root):
    with pytest.raises(SpecfloError, match="rob has no token named 'laptop'"):
        auth.revoke_user_token(root, "rob", "laptop")

    result = _token(root, "revoke", "rob", "laptop")
    assert result.exit_code == 1
    assert "no token named 'laptop'" in result.output


# --- resolving --------------------------------------------------------------


def test_whoami_returns_the_user_and_the_role_read_live(root, client):
    secret = auth.mint_user_token(root, "rob", "laptop")

    response = client.get(routes.WHOAMI_PATH, headers=_bearer(secret))
    assert response.status_code == 200
    assert response.json() == {"identity": "developer", "user": "rob", "role": "developer"}

    auth.set_role(root, "rob", "requester")

    response = client.get(routes.WHOAMI_PATH, headers=_bearer(secret))
    assert response.json() == {"identity": "requester", "user": "rob", "role": "requester"}


def test_a_removed_users_token_gets_401(root, client):
    secret = auth.mint_user_token(root, "rob", "laptop")

    auth.remove_user(root, "rob")

    assert auth.principal_for(root, secret) is None
    assert client.get(routes.WHOAMI_PATH, headers=_bearer(secret)).status_code == 401


def test_a_token_whose_user_row_is_gone_resolves_to_nobody(root):
    secret = auth.mint_user_token(root, "rob", "laptop")
    with open_store(root) as store:
        store.connection.execute("DELETE FROM users WHERE name = 'rob'")
        store.connection.commit()

    assert auth.principal_for(root, secret) is None


def test_an_unknown_or_empty_secret_resolves_to_nobody(root):
    assert auth.principal_for(root, "sfp_nothing") is None
    assert auth.principal_for(root, "") is None


def test_ten_calls_in_a_minute_write_last_used_once(root, client, clock, touches):
    secret = auth.mint_user_token(root, "rob", "laptop")

    for _ in range(10):
        assert client.get(routes.WHOAMI_PATH, headers=_bearer(secret)).status_code == 200
        clock.move(5)

    assert len(touches) == 1
    [token] = _stored(root, "rob")
    first = datetime.datetime.fromtimestamp(1_800_000_000, datetime.timezone.utc)
    assert token.last_used == first.isoformat(timespec="seconds")


def test_a_call_a_minute_later_moves_last_used(root, client, clock, touches):
    secret = auth.mint_user_token(root, "rob", "laptop")
    client.get(routes.WHOAMI_PATH, headers=_bearer(secret))

    clock.move(59)
    client.get(routes.WHOAMI_PATH, headers=_bearer(secret))
    assert len(touches) == 1

    clock.move(1)
    client.get(routes.WHOAMI_PATH, headers=_bearer(secret))

    assert len(touches) == 2
    [token] = _stored(root, "rob")
    later = datetime.datetime.fromtimestamp(1_800_000_060, datetime.timezone.utc)
    assert token.last_used == later.isoformat(timespec="seconds")


def test_an_operation_route_acts_under_the_tokens_role(root, client):
    secret = auth.mint_user_token(root, "rob", "laptop")

    response = client.post(wire.route_path("list_projects"), headers=_bearer(secret), json={})

    assert response.status_code == 200, response.text


# --- the older tokens file --------------------------------------------------


def test_tokens_from_the_older_file_still_authenticate(root, client):
    for identity in auth.IDENTITIES:
        secret = auth.mint_token(root, identity)

        assert auth.principal_for(root, secret) == (identity, identity)
        response = client.get(routes.WHOAMI_PATH, headers=_bearer(secret))
        assert response.status_code == 200
        assert response.json() == {"identity": identity, "user": identity, "role": identity}
