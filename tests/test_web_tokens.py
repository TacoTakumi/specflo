"""The user page's tokens: a signed-in user creates, lists and revokes their own personal tokens.

A new token's secret shows once, on the page the create answers with, and
that response is never cached; the store keeps only its hash, so no later
page can show it again. The list holds the signed-in user's tokens and no
one else's. A revoke names a token and acts on the signed-in user's own
tokens alone, so another user's token name is simply not found. Every post
runs the session-secret guard first, and a refused post changes nothing.
"""

from __future__ import annotations

import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import auth, routes, web
from specflo.daemon.app import create_app

from conftest import web_sign_in

NOT_THIS_SESSION = "That form did not come from this session."
SECRET = re.compile(r"sfp_[A-Za-z0-9_-]{20,}")


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


def post(client, path, **fields):
    return client.post(path, data={"session": client.cookies[web.SESSION_COOKIE], **fields})


def create(client, name):
    return post(client, web.USER_PATH, op=web.ADD_TOKEN_OP, name=name)


def revoke(client, name):
    return post(client, web.USER_PATH, op=web.REVOKE_TOKEN_OP, name=name)


def whoami(client, secret):
    return client.get(routes.WHOAMI_PATH, headers={"Authorization": f"Bearer {secret}"})


def names(root, owner):
    return [token.name for token in auth.list_user_tokens(root, owner)]


def rows(html):
    """The token rows of the page, by the name each one carries."""
    return {
        unescape(name): body
        for name, body in re.findall(r'<tr data-token="([^"]*)">(.*?)</tr>', html, re.S)
    }


# --- the page --------------------------------------------------------------------


@pytest.mark.parametrize("role", ["developer", "requester"])
def test_the_header_links_to_the_page_for_a_signed_in_user(app, root, role):
    client = signed_in(app, root, role, role)

    html = client.get(web.HOME_PATH).text

    header = re.search(r'<header class="site-header">.*?</header>', html, re.S).group(0)
    assert f'<a class="identity" href="{web.USER_PATH}">{role}</a>' in header


def test_the_sign_in_page_has_no_link(app):
    client = TestClient(app, follow_redirects=False)

    assert f'href="{web.USER_PATH}"' not in client.get(web.SIGNIN_PATH).text


def test_a_browser_with_no_session_is_sent_to_sign_in(app):
    client = TestClient(app, follow_redirects=False)

    for response in (
        client.get(web.USER_PATH),
        client.get(web.OLD_TOKENS_PATH),
        client.post(web.USER_PATH, data={"op": web.ADD_TOKEN_OP, "name": "laptop"}),
        client.post(web.USER_PATH, data={"op": web.REVOKE_TOKEN_OP, "name": "laptop"}),
    ):
        assert response.status_code == 303
        assert response.headers["location"] == web.SIGNIN_PATH


def test_the_page_lists_only_the_users_own_tokens(app, root):
    client = signed_in(app, root)
    signed_in(app, root, "ann")
    auth.mint_user_token(root, "rob", "laptop", today="2026-03-01")
    auth.mint_user_token(root, "ann", "desktop")

    html = client.get(web.USER_PATH).text

    shown = rows(html)
    assert list(shown) == ["laptop"]
    (token,) = auth.list_user_tokens(root, "rob")
    assert "2026-03-01" in shown["laptop"]
    assert "never" in shown["laptop"]
    assert token.last4 in shown["laptop"]
    assert "desktop" not in html


def test_a_used_token_shows_when_it_was_last_used(app, root):
    client = signed_in(app, root)
    secret = auth.mint_user_token(root, "rob", "laptop")
    assert whoami(client, secret).status_code == 200
    (token,) = auth.list_user_tokens(root, "rob")

    shown = rows(client.get(web.USER_PATH).text)

    assert token.last_used in shown["laptop"]
    assert "never" not in shown["laptop"]


def test_with_no_token_the_page_says_so(app, root):
    client = signed_in(app, root)

    html = client.get(web.USER_PATH).text

    assert rows(html) == {}
    assert web.NO_TOKENS in html


# --- create ----------------------------------------------------------------------


def test_creating_a_token_shows_its_secret_once_and_it_acts_as_the_user(app, root):
    client = signed_in(app, root)

    response = create(client, "laptop")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    (secret,) = set(SECRET.findall(response.text))
    assert web.SHOWN_ONCE in response.text
    assert "laptop" in rows(response.text)
    assert auth.principal_for(root, secret) == ("rob", "developer")
    assert whoami(client, secret).json()["user"] == "rob"
    # a reload shows the token and never its secret again
    again = client.get(web.USER_PATH).text
    assert "laptop" in rows(again)
    assert secret not in again
    assert not SECRET.search(again)
    assert web.SHOWN_ONCE not in again


def test_creating_is_audited_with_the_user_and_the_token_name(app, root):
    client = signed_in(app, root)

    create(client, "laptop")

    log = (root / routes.AUDIT_FILENAME).read_text()
    assert '"user": "rob"' in log and '"operation": "add_token"' in log and '"id": "laptop"' in log


def test_a_name_already_used_is_refused_and_the_token_kept(app, root):
    client = signed_in(app, root)
    secret = auth.mint_user_token(root, "rob", "laptop")

    response = create(client, "laptop")

    assert response.status_code == 409
    assert "rob already has a token named 'laptop'." in unescape(response.text)
    assert not SECRET.search(response.text)
    assert names(root, "rob") == ["laptop"]
    assert auth.principal_for(root, secret) == ("rob", "developer")


@pytest.mark.parametrize("name", ["", "   "])
def test_a_blank_name_is_refused(app, root, name):
    client = signed_in(app, root)

    response = create(client, name)

    assert response.status_code == 400
    assert names(root, "rob") == []


# --- revoke ----------------------------------------------------------------------


def test_revoking_an_own_token_ends_it(app, root):
    client = signed_in(app, root)
    secret = auth.mint_user_token(root, "rob", "laptop")
    auth.mint_user_token(root, "rob", "desktop")

    response = revoke(client, "laptop")

    assert response.status_code == 303
    assert response.headers["location"] == web.TOKENS_SECTION
    assert names(root, "rob") == ["desktop"]
    assert whoami(client, secret).status_code == 401
    assert list(rows(client.get(web.USER_PATH).text)) == ["desktop"]
    log = (root / routes.AUDIT_FILENAME).read_text()
    assert '"operation": "revoke_token"' in log


def test_revoking_another_users_token_name_is_refused_and_it_stays_valid(app, root):
    client = signed_in(app, root)
    signed_in(app, root, "ann")
    theirs = auth.mint_user_token(root, "ann", "desktop")

    response = revoke(client, "desktop")

    assert response.status_code == 404
    assert "rob has no token named 'desktop'." in unescape(response.text)
    assert names(root, "ann") == ["desktop"]
    assert auth.principal_for(root, theirs) == ("ann", "developer")
    assert whoami(client, theirs).json()["user"] == "ann"


def test_each_token_has_a_revoke_form_for_its_name(app, root):
    client = signed_in(app, root)
    auth.mint_user_token(root, "rob", "laptop")

    body = rows(client.get(web.USER_PATH).text)["laptop"]

    (form,) = re.findall(r"<form[^>]*>.*?</form>", body, re.S)
    assert f'action="{web.USER_PATH}"' in form
    assert f'name="op" value="{web.REVOKE_TOKEN_OP}"' in form
    assert 'name="name" value="laptop"' in form
    assert f'name="session" value="{client.cookies[web.SESSION_COOKIE]}"' in form


# --- the session guard -----------------------------------------------------------


@pytest.mark.parametrize("fields", [{}, {"session": "not-the-secret"}])
def test_a_create_without_the_session_secret_changes_nothing(app, root, fields):
    client = signed_in(app, root)

    response = client.post(web.USER_PATH, data={"op": web.ADD_TOKEN_OP, "name": "laptop", **fields})

    assert response.status_code == 403
    assert NOT_THIS_SESSION in response.text
    assert not SECRET.search(response.text)
    assert names(root, "rob") == []


@pytest.mark.parametrize("fields", [{}, {"session": "not-the-secret"}])
def test_a_revoke_without_the_session_secret_changes_nothing(app, root, fields):
    client = signed_in(app, root)
    secret = auth.mint_user_token(root, "rob", "laptop")

    response = client.post(web.USER_PATH, data={"op": web.REVOKE_TOKEN_OP, "name": "laptop", **fields})

    assert response.status_code == 403
    assert NOT_THIS_SESSION in response.text
    assert names(root, "rob") == ["laptop"]
    assert auth.principal_for(root, secret) == ("rob", "developer")
