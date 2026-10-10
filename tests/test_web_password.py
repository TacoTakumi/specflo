"""The user page's change password form, for a browser signed in with a password.

A session records how it was opened. One opened with a password has the
form on the user page; one opened through a proxy has none, and a post
from it gets a 404. The old change password page leads to the form. The form asks for the current
password and the new one twice. A wrong current password, new passwords
that differ and an empty new password are refused beside the form, and the
password stays as it was. A wrong current password is not a failed sign-in:
it neither counts toward the lockout nor is blocked by one. A change keeps
the session that made it and ends every other session of the user. Every
post runs the session-secret guard first.
"""

from __future__ import annotations

import time
from html import unescape

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import auth, routes, web
from specflo.daemon.app import create_app
from specflo.daemon.store import open_store

from conftest import WEB_PASSWORD, web_sign_in

NEW = "a new and longer passphrase"
NOT_THIS_SESSION = "That form did not come from this session."


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def app(root):
    return create_app(root)


def signed_in(app, root, name="rob", role="developer"):
    client = TestClient(app, follow_redirects=False)
    web_sign_in(client, root, role, name)
    return client


def proxy_signed_in(app, root, name="rob", role="developer"):
    """A client on a session opened as a proxy sign-in opens one: no password asked."""
    web_sign_in(TestClient(app, follow_redirects=False), root, role, name)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(web.SESSION_COOKIE, auth.open_session(root, name, "proxy", time.time()))
    return client


def change(client, current=WEB_PASSWORD, new=NEW, again=None, **extra):
    fields = {
        "session": client.cookies[web.SESSION_COOKIE],
        "current": current, "new": new, "again": new if again is None else again,
    }
    return client.post(web.PASSWORD_PATH, data={**fields, **extra})


def signs_in(app, password, name="rob") -> bool:
    client = TestClient(app, follow_redirects=False)
    response = client.post(web.SIGNIN_PATH, data={"name": name, "password": password})
    return response.status_code == 303


def password_hash(root, name="rob"):
    with open_store(root) as store:
        return store.get_user(name).password_hash


def failures(root, name="rob"):
    with open_store(root) as store:
        return store.list_signin_failures(name)


# --- who has the page ------------------------------------------------------------


def test_a_password_session_has_the_form(app, root):
    client = signed_in(app, root)

    html = client.get(web.USER_PATH).text
    assert '<section id="password">' in html
    assert f'action="{web.PASSWORD_PATH}"' in html
    for field in ("current", "new", "again"):
        assert f'name="{field}"' in html
    assert f'name="session" value="{client.cookies[web.SESSION_COOKIE]}"' in html


def test_a_proxy_session_has_no_form_and_its_post_gets_404(app, root):
    client = proxy_signed_in(app, root)
    before = password_hash(root)

    page = client.get(web.USER_PATH)
    assert page.status_code == 200
    assert '<section id="password">' not in page.text
    assert web.PASSWORD_PATH not in page.text
    assert change(client).status_code == 404
    assert password_hash(root) == before


def test_a_browser_with_no_session_is_sent_to_sign_in(app):
    client = TestClient(app, follow_redirects=False)

    for response in (client.get(web.OLD_PASSWORD_PATH), client.post(web.PASSWORD_PATH, data={})):
        assert response.status_code == 303
        assert response.headers["location"] == web.SIGNIN_PATH


# --- refusals --------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"current": "not the password"}, web.WRONG_PASSWORD),
        ({"again": "something else"}, web.PASSWORDS_DIFFER),
        ({"new": "", "again": ""}, web.PASSWORD_EMPTY),
    ],
)
def test_a_refused_change_keeps_the_password(app, root, fields, message):
    client = signed_in(app, root)
    before = password_hash(root)

    response = change(client, **fields)

    assert response.status_code == 400
    assert message in unescape(response.text)
    assert 'name="current"' in response.text
    assert password_hash(root) == before
    assert signs_in(app, WEB_PASSWORD)
    assert client.get(web.HOME_PATH).status_code == 200


def test_a_wrong_current_password_is_no_failed_sign_in(app, root):
    client = signed_in(app, root)

    for _ in range(auth.SIGNIN_LOCK_FAILURES + 1):
        assert change(client, current="not the password").status_code == 400

    assert failures(root) == []
    assert signs_in(app, WEB_PASSWORD)


def test_a_lockout_does_not_block_a_change(app, root):
    client = signed_in(app, root)
    for _ in range(auth.SIGNIN_LOCK_FAILURES):
        assert not signs_in(app, "not the password")
    assert not signs_in(app, WEB_PASSWORD)

    assert change(client).status_code == 200
    # the change clears the failures, as a password set by the operator does
    assert signs_in(app, NEW)


@pytest.mark.parametrize("fields", [{}, {"session": "not-the-secret"}])
def test_a_post_without_the_session_secret_changes_nothing(app, root, fields):
    client = signed_in(app, root)
    before = password_hash(root)

    response = client.post(
        web.PASSWORD_PATH,
        data={"current": WEB_PASSWORD, "new": NEW, "again": NEW, **fields},
    )

    assert response.status_code == 403
    assert NOT_THIS_SESSION in response.text
    assert password_hash(root) == before


# --- the change ------------------------------------------------------------------


def test_a_change_keeps_this_session_ends_the_others_and_takes_the_new_password(app, root):
    client = signed_in(app, root)
    other = signed_in(app, root)
    ann = signed_in(app, root, "ann")

    response = change(client)

    assert response.status_code == 200
    assert web.PASSWORD_CHANGED in response.text
    assert NEW not in response.text and WEB_PASSWORD not in response.text
    # this session still works; the user's other one goes to sign-in
    assert client.get(web.HOME_PATH).status_code == 200
    gone = other.get(web.HOME_PATH)
    assert gone.status_code == 303 and gone.headers["location"] == web.SIGNIN_PATH
    # another user's session is not theirs to end
    assert ann.get(web.HOME_PATH).status_code == 200
    assert signs_in(app, NEW)
    assert not signs_in(app, WEB_PASSWORD)


def test_a_change_is_audited_without_a_secret(app, root):
    client = signed_in(app, root)

    change(client)

    log = (root / routes.AUDIT_FILENAME).read_text()
    assert '"user": "rob"' in log and '"operation": "change_password"' in log
    assert NEW not in log and WEB_PASSWORD not in log
    assert password_hash(root) not in log
