"""The web UI's front door: sign in as one identity, then browse on a session.

A browser reaches the daemon's pages through a session cookie, not a bearer
token. Without a live session every page sends the browser to the sign-in
page, and nothing else is served but that page and the assets it needs.
Signing in as requester or developer with that identity's token sets the
cookie; the pages then show who is signed in. The session lives only in the
daemon process, so a restart signs every browser out; the bearer token never
reaches the browser and the cookie never unlocks the API.
"""

import re

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import auth, routes, web
from specflo.daemon.app import create_app


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def client(root):
    return TestClient(create_app(root), follow_redirects=False)


def sign_in(client, root, identity):
    """Sign ``client`` in as ``identity`` with a freshly minted token."""
    token = auth.mint_token(root, identity)
    response = client.post(web.SIGNIN_PATH, data={"identity": identity, "token": token})
    assert response.status_code == 303, response.text
    return response


def script_sources(html):
    return re.findall(r"<script[^>]*\ssrc=\"([^\"]*)\"", html)


# --- without a session ------------------------------------------------------


def test_a_page_without_a_session_redirects_to_sign_in(client):
    response = client.get(web.HOME_PATH)

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH


def test_a_cookie_the_daemon_never_issued_redirects_to_sign_in(client):
    client.cookies.set(web.SESSION_COOKIE, "made-up")

    response = client.get(web.HOME_PATH)

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH


def test_the_sign_in_page_offers_both_identities_and_a_token_field(client):
    response = client.get(web.SIGNIN_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert f'action="{web.SIGNIN_PATH}"' in html and 'method="post"' in html
    for identity in auth.IDENTITIES:
        assert f'value="{identity}"' in html
    assert 'name="token"' in html and 'type="password"' in html
    assert "signed in as" not in html


def test_the_assets_directory_is_served_without_a_session(client):
    response = client.get(f"{web.ASSETS_PATH}/htmx.min.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "htmx" in response.text


def test_every_script_on_a_rendered_page_comes_from_the_assets_directory(client, root):
    pages = [client.get(web.SIGNIN_PATH).text]
    sign_in(client, root, "developer")
    pages.append(client.get(web.HOME_PATH).text)

    for html in pages:
        sources = script_sources(html)
        assert sources, "a rendered page ships no script"
        assert all(src.startswith(web.ASSETS_PATH + "/") for src in sources), sources
        assert "<script>" not in html


# --- signing in -------------------------------------------------------------


@pytest.mark.parametrize("identity", auth.IDENTITIES)
def test_signing_in_sets_a_session_cookie_and_the_pages_show_the_identity(
    client, root, identity
):
    response = sign_in(client, root, identity)

    assert response.headers["location"] == web.HOME_PATH
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{web.SESSION_COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie

    page = client.get(web.HOME_PATH)

    assert page.status_code == 200
    assert "signed in as" in page.text and identity in page.text
    other = next(name for name in auth.IDENTITIES if name != identity)
    assert other not in page.text


def test_the_session_cookie_is_not_the_bearer_token(client, root):
    token = auth.mint_token(root, "developer")

    response = client.post(web.SIGNIN_PATH, data={"identity": "developer", "token": token})

    assert response.status_code == 303
    assert token not in response.headers["set-cookie"]


def test_a_signed_in_browser_asking_for_sign_in_goes_home(client, root):
    sign_in(client, root, "requester")

    response = client.get(web.SIGNIN_PATH)

    assert response.status_code == 303
    assert response.headers["location"] == web.HOME_PATH


def test_a_wrong_token_re_renders_sign_in_with_no_cookie(client, root):
    auth.mint_token(root, "developer")

    response = client.post(
        web.SIGNIN_PATH, data={"identity": "developer", "token": "not-a-token"}
    )

    assert response.status_code == 400
    assert "set-cookie" not in response.headers
    assert web.SIGNIN_FAILED in response.text
    assert 'name="token"' in response.text
    assert client.get(web.HOME_PATH).status_code == 303


def test_a_token_minted_for_the_other_identity_does_not_sign_in(client, root):
    token = auth.mint_token(root, "developer")

    response = client.post(web.SIGNIN_PATH, data={"identity": "requester", "token": token})

    assert response.status_code == 400
    assert "set-cookie" not in response.headers


def test_an_identity_outside_the_two_does_not_sign_in(client, root):
    token = auth.mint_token(root, "developer")

    response = client.post(web.SIGNIN_PATH, data={"identity": "admin", "token": token})

    assert response.status_code == 400
    assert "set-cookie" not in response.headers


def test_a_form_missing_its_fields_does_not_sign_in(client):
    response = client.post(web.SIGNIN_PATH, data={})

    assert response.status_code == 400
    assert "set-cookie" not in response.headers


# --- the session's reach ----------------------------------------------------


def test_the_session_cookie_never_unlocks_the_api(client, root):
    sign_in(client, root, "developer")

    assert client.get(routes.WHOAMI_PATH).status_code == 401
    assert client.get(daemon.HEALTH_PATH).status_code == 200


def test_a_restarted_daemon_knows_no_earlier_session(client, root):
    sign_in(client, root, "developer")
    restarted = TestClient(create_app(root), follow_redirects=False)
    restarted.cookies.set(web.SESSION_COOKIE, client.cookies[web.SESSION_COOKIE])

    assert client.get(web.HOME_PATH).status_code == 200
    assert restarted.get(web.HOME_PATH).status_code == 303
