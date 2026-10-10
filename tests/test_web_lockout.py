"""Password sign-in locks a user out after five failures in fifteen minutes.

Five wrong passwords for one user within fifteen minutes refuse that user's
password sign-in for fifteen minutes after the fifth, even with the right
password, with a message of its own. The failures live in the state store,
so the lockout holds across a restart; a right password clears them, and so
does 'user passwd'. Only a user with a password counts failures: an unknown
name or a proxy-only user has nothing to lock. Bearer tokens never consult
the lockout.
"""

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import daemon
from specflo.cli import app
from specflo.daemon import auth, routes, web
from specflo.daemon.app import create_app
from specflo.daemon.store import open_store

from conftest import role_token

PASSWORD = "correct horse battery staple"
WRONG = "not the password"
MINUTE = 60


@pytest.fixture
def root(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")
    auth.add_user(root, "rob", "developer", PASSWORD)
    return root


@pytest.fixture
def client(root):
    return TestClient(create_app(root), follow_redirects=False)


@pytest.fixture
def clock(monkeypatch):
    """The web UI's clock, held still at a fixed moment; ``move`` sets it forward."""

    class Clock:
        now = 1_800_000_000.0

        def move(self, seconds):
            self.now += seconds

    held = Clock()
    monkeypatch.setattr(web, "_now", lambda: held.now)
    return held


def attempt(client, password, name="rob"):
    return client.post(web.SIGNIN_PATH, data={"name": name, "password": password})


def fail(client, times, name="rob"):
    """Post ``times`` wrong passwords for ``name``; the last response."""
    for _ in range(times):
        response = attempt(client, WRONG, name)
        assert response.status_code in (400, 429), response.text
    return response


def assert_locked(response):
    assert response.status_code == 429
    assert web.SIGNIN_LOCKED in response.text
    assert web.SIGNIN_FAILED not in response.text
    assert "set-cookie" not in response.headers


def failures(root, name="rob"):
    with open_store(root) as store:
        return store.list_signin_failures(name)


# --- the lockout -------------------------------------------------------------


def test_four_wrong_passwords_lock_nothing(client, clock):
    response = fail(client, 4)

    assert response.status_code == 400
    assert web.SIGNIN_FAILED in response.text
    assert attempt(client, PASSWORD).status_code == 303


def test_five_wrong_passwords_then_the_right_one_is_refused_with_the_lockout_message(client, clock):
    fail(client, 5)

    assert_locked(attempt(client, PASSWORD))
    assert client.get(web.HOME_PATH).status_code == 303


def test_the_fifth_wrong_password_already_says_the_sign_in_is_locked(client, clock):
    fail(client, 4)

    assert_locked(attempt(client, WRONG))


def test_the_lockout_ends_fifteen_minutes_after_the_fifth_failure(client, clock):
    fail(client, 4)
    clock.move(10 * MINUTE)
    fail(client, 1)

    clock.move(15 * MINUTE - 1)
    assert_locked(attempt(client, PASSWORD))

    clock.move(1)
    assert attempt(client, PASSWORD).status_code == 303


def test_attempts_while_locked_do_not_stretch_the_lockout(client, clock):
    fail(client, 5)
    clock.move(10 * MINUTE)
    assert_locked(attempt(client, WRONG))

    clock.move(5 * MINUTE)

    assert attempt(client, PASSWORD).status_code == 303


def test_failures_older_than_fifteen_minutes_do_not_count(client, clock):
    fail(client, 4)
    clock.move(15 * MINUTE)
    fail(client, 1)

    assert attempt(client, PASSWORD).status_code == 303


def test_a_right_password_clears_the_failures(client, root, clock):
    fail(client, 4)
    assert attempt(client, PASSWORD).status_code == 303
    assert failures(root) == []

    response = fail(TestClient(client.app, follow_redirects=False), 4)

    assert response.status_code == 400


def test_another_user_is_not_locked(client, root, clock):
    auth.add_user(root, "ann", "requester", PASSWORD)
    fail(client, 5)

    assert attempt(client, PASSWORD, name="ann").status_code == 303


def test_the_name_locks_in_any_case(client, clock):
    fail(client, 5, name="Rob")

    assert_locked(attempt(client, PASSWORD, name="rob"))


@pytest.mark.parametrize("name", ("nobody", "ann"), ids=("unknown", "proxy-only"))
def test_a_name_with_no_password_has_nothing_to_lock(client, root, clock, name):
    auth.add_user(root, "ann", "requester", None)

    response = fail(client, 6, name=name)

    assert response.status_code == 400
    assert web.SIGNIN_FAILED in response.text
    assert failures(root, name) == []


# --- what the lockout survives and what clears it ----------------------------


def test_the_lockout_holds_across_a_restart(client, root, clock):
    fail(client, 5)
    restarted = TestClient(create_app(root), follow_redirects=False)

    assert_locked(attempt(restarted, PASSWORD))


def test_a_new_password_signs_in_at_once(client, root, clock):
    fail(client, 5)

    auth.set_password(root, "rob", "a new password")

    assert failures(root) == []
    assert attempt(client, "a new password").status_code == 303


def test_user_passwd_on_the_command_line_clears_the_lockout(client, root, clock):
    fail(client, 5)

    result = CliRunner().invoke(
        app, ["serve", "--root", str(root), "user", "passwd", "rob"],
        input="a new password\na new password\n",
    )

    assert result.exit_code == 0, result.output
    assert attempt(client, "a new password").status_code == 303


def test_clearing_the_password_clears_the_failures(client, root, clock):
    fail(client, 5)

    auth.set_password(root, "rob", None)

    assert failures(root) == []


def test_removing_the_user_removes_their_failures(client, root, clock):
    fail(client, 3)

    auth.remove_user(root, "rob")

    assert failures(root) == []


def test_a_token_still_works_while_the_user_is_locked(client, root, clock):
    fail(client, 5)
    token = role_token(root, "developer")

    response = client.get(routes.WHOAMI_PATH, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert_locked(attempt(client, PASSWORD))
