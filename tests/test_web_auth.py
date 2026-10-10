"""The web UI's front door: sign in with a user name and password, then browse on a session.

A browser reaches the daemon's pages through a session cookie, not a bearer
token. Without a live session every page sends the browser to the sign-in
page, and nothing else is served but that page and the assets it needs.
Signing in with a user's name and password sets the cookie; the pages then
show who is signed in. A wrong password, an unknown name and a user with no
password all get one failure message, so the page never tells which. The
session lives in the state store by the hash of its secret, so it survives a
restart; it lasts 12 hours past its last use, and a request writes its row
at most once a minute. The cookie never unlocks the API.
"""

import re

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import auth, routes, web
from specflo.daemon.app import create_app
from specflo.daemon.store import SqliteStore, open_store

from conftest import WEB_PASSWORD, web_sign_in


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


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


def script_sources(html):
    return re.findall(r"<script[^>]*\ssrc=\"([^\"]*)\"", html)


def session_of(client, root):
    """The session row the client's cookie stands for, or None."""
    secret = client.cookies.get(web.SESSION_COOKIE)
    if not secret:
        return None
    with open_store(root) as store:
        return store.get_session(auth.hash_token(secret))


def failure_message(html):
    return re.search(r'<p class="error" role="alert">(.*?)</p>', html, re.S).group(1).strip()


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


def test_the_sign_in_page_asks_for_a_user_name_and_a_password(client):
    response = client.get(web.SIGNIN_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert f'action="{web.SIGNIN_PATH}"' in html and 'method="post"' in html
    assert 'name="name"' in html
    assert 'name="password"' in html and 'type="password"' in html
    assert 'name="identity"' not in html and 'name="token"' not in html
    assert "signed in as" not in html


def test_a_fresh_root_says_no_user_exists_and_names_the_command(client):
    html = client.get(web.SIGNIN_PATH).text

    assert "No user exists yet" in html
    assert "specflo serve user add" in html


def test_a_root_with_a_user_does_not_show_the_first_run_hint(client, root):
    auth.add_user(root, "rob", "developer", WEB_PASSWORD)

    html = client.get(web.SIGNIN_PATH).text

    assert "No user exists yet" not in html
    assert "specflo serve user add" not in html


def test_the_assets_directory_is_served_without_a_session(client):
    response = client.get(f"{web.ASSETS_PATH}/htmx.min.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "htmx" in response.text


def test_every_script_on_a_rendered_page_comes_from_the_assets_directory(client, root):
    pages = [client.get(web.SIGNIN_PATH).text]
    web_sign_in(client, root)
    pages.append(client.get(web.HOME_PATH).text)

    for html in pages:
        sources = script_sources(html)
        assert sources, "a rendered page ships no script"
        assert all(src.startswith(web.ASSETS_PATH + "/") for src in sources), sources
        assert "<script>" not in html


# --- signing in -------------------------------------------------------------


@pytest.mark.parametrize("role", ("requester", "developer"))
def test_signing_in_sets_a_session_cookie_and_the_pages_show_the_role(client, root, role):
    response = web_sign_in(client, root, role)

    assert response.headers["location"] == web.HOME_PATH
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{web.SESSION_COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Max-Age=43200" in cookie and web.SESSION_TTL == 43200

    page = client.get(web.HOME_PATH)

    assert page.status_code == 200
    assert "signed in as" in page.text and role in page.text
    other = next(name for name in ("requester", "developer") if name != role)
    assert other not in page.text


def test_the_name_is_matched_in_any_case(client, root):
    auth.add_user(root, "rob", "developer", WEB_PASSWORD)

    response = client.post(web.SIGNIN_PATH, data={"name": "Rob", "password": WEB_PASSWORD})

    assert response.status_code == 303
    assert session_of(client, root).user == "rob"


def test_the_session_row_holds_the_hash_user_method_and_expiry_and_no_secret(
    client, root, clock
):
    auth.add_user(root, "rob", "developer", WEB_PASSWORD)

    client.post(web.SIGNIN_PATH, data={"name": "rob", "password": WEB_PASSWORD})

    secret = client.cookies[web.SESSION_COOKIE]
    session = session_of(client, root)
    assert session.hash == auth.hash_token(secret) and session.hash != secret
    assert session.user == "rob"
    assert session.method == "password"
    assert session.expires == clock.now + web.SESSION_TTL
    assert session.written == clock.now
    stored = (root / daemon.STATE_STORE_FILENAME).read_bytes()
    assert secret.encode() not in stored
    assert WEB_PASSWORD.encode() not in stored


def test_the_session_cookie_is_neither_the_password_nor_a_token(client, root):
    response = web_sign_in(client, root)

    assert WEB_PASSWORD not in response.headers["set-cookie"]
    assert auth.identity_for(root, client.cookies[web.SESSION_COOKIE]) is None


def test_a_signed_in_browser_asking_for_sign_in_goes_home(client, root):
    web_sign_in(client, root, "requester")

    response = client.get(web.SIGNIN_PATH)

    assert response.status_code == 303
    assert response.headers["location"] == web.HOME_PATH


def test_signing_in_drops_the_sessions_already_expired(client, root, clock):
    web_sign_in(client, root)
    stale = session_of(client, root)
    clock.move(web.SESSION_TTL + 1)

    web_sign_in(TestClient(client.app, follow_redirects=False), root)

    with open_store(root) as store:
        assert store.get_session(stale.hash) is None


def test_wrong_password_unknown_name_and_proxy_only_user_fail_alike(client, root):
    auth.add_user(root, "rob", "developer", WEB_PASSWORD)
    auth.add_user(root, "ann", "requester", None)
    attempts = {
        "a wrong password": {"name": "rob", "password": "not the password"},
        "an unknown name": {"name": "nobody", "password": WEB_PASSWORD},
        "a proxy-only user": {"name": "ann", "password": WEB_PASSWORD},
    }

    messages = set()
    for attempt, form in attempts.items():
        response = client.post(web.SIGNIN_PATH, data=form)

        assert response.status_code == 400, attempt
        assert "set-cookie" not in response.headers, attempt
        assert 'name="password"' in response.text, attempt
        messages.add(failure_message(response.text))
    assert messages == {web.SIGNIN_FAILED}
    assert client.get(web.HOME_PATH).status_code == 303


def test_the_agent_cannot_sign_in_with_its_token(client, root):
    token = auth.mint_token(root, "agent")

    response = client.post(web.SIGNIN_PATH, data={"name": "agent", "password": token})

    assert response.status_code == 400
    assert "set-cookie" not in response.headers
    assert failure_message(response.text) == web.SIGNIN_FAILED


def test_a_form_missing_its_fields_does_not_sign_in(client):
    response = client.post(web.SIGNIN_PATH, data={})

    assert response.status_code == 400
    assert "set-cookie" not in response.headers


def test_an_old_parameter_hash_is_rehashed_on_sign_in(client, root):
    auth.add_user(root, "rob", "developer", None)
    salt = bytes(16)
    old = auth._scrypt(WEB_PASSWORD, salt, 8192, 8, 5, 32)
    with open_store(root) as store:
        store.set_user_password("rob", f"scrypt$8192$8$5${salt.hex()}${old.hex()}")

    response = client.post(web.SIGNIN_PATH, data={"name": "rob", "password": WEB_PASSWORD})

    assert response.status_code == 303
    stored = auth.require_user(root, "rob").password_hash
    assert stored.startswith("scrypt$16384$")
    assert not auth.needs_rehash(stored)
    assert auth.verify_password(WEB_PASSWORD, stored)


def test_a_current_hash_is_left_as_it_is(client, root):
    before = auth.add_user(root, "rob", "developer", WEB_PASSWORD).password_hash

    client.post(web.SIGNIN_PATH, data={"name": "rob", "password": WEB_PASSWORD})

    assert auth.require_user(root, "rob").password_hash == before


# --- the session's reach ----------------------------------------------------


def test_the_session_cookie_never_unlocks_the_api(client, root):
    web_sign_in(client, root)

    assert client.get(routes.WHOAMI_PATH).status_code == 401
    assert client.get(daemon.HEALTH_PATH).status_code == 200


def test_a_session_survives_a_restart(client, root):
    web_sign_in(client, root)
    restarted = TestClient(create_app(root), follow_redirects=False)
    restarted.cookies.set(web.SESSION_COOKIE, client.cookies[web.SESSION_COOKIE])

    page = restarted.get(web.HOME_PATH)

    assert page.status_code == 200
    assert "signed in as" in page.text


def test_a_page_acts_under_the_role_the_user_holds_now(client, root):
    web_sign_in(client, root, "developer", name="rob")
    auth.set_role(root, "rob", "requester")

    page = client.get(web.HOME_PATH)

    assert page.status_code == 200
    assert "requester" in page.text and "developer" not in page.text


def test_a_removed_user_is_sent_to_sign_in(client, root):
    web_sign_in(client, root, "developer", name="rob")
    auth.remove_user(root, "rob")

    response = client.get(web.HOME_PATH)

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH


def test_the_cookie_is_marked_secure_only_when_the_page_came_over_https(root):
    auth.add_user(root, "rob", "developer", WEB_PASSWORD)
    form = {"name": "rob", "password": WEB_PASSWORD}

    plain = TestClient(create_app(root), follow_redirects=False)
    assert "Secure" not in plain.post(web.SIGNIN_PATH, data=form).headers["set-cookie"]

    tls = TestClient(create_app(root), base_url="https://testserver", follow_redirects=False)
    assert "Secure" in tls.post(web.SIGNIN_PATH, data=form).headers["set-cookie"]


# --- the sliding expiry -----------------------------------------------------


@pytest.fixture
def writes(monkeypatch):
    """Every session row write the store makes, as (hash, expires, written)."""
    seen = []
    real = SqliteStore.update_session

    def update_session(self, session_hash, *, expires, written):
        seen.append((session_hash, expires, written))
        return real(self, session_hash, expires=expires, written=written)

    monkeypatch.setattr(SqliteStore, "update_session", update_session)
    return seen


def test_requests_within_a_minute_of_sign_in_write_nothing(client, root, clock, writes):
    web_sign_in(client, root)
    opened = session_of(client, root)

    for _ in range(10):
        clock.move(5)
        response = client.get(web.HOME_PATH)
        assert response.status_code == 200
        assert "set-cookie" not in response.headers

    assert writes == []
    assert session_of(client, root) == opened


def test_ten_requests_within_a_minute_write_the_row_once(client, root, clock, writes):
    web_sign_in(client, root)
    clock.move(61)
    extended = clock.now

    for _ in range(10):
        assert client.get(web.HOME_PATH).status_code == 200
        clock.move(5)

    assert len(writes) == 1
    session = session_of(client, root)
    assert session.written == extended
    assert session.expires == extended + web.SESSION_TTL


def test_the_extending_response_refreshes_the_cookie(client, root, clock):
    web_sign_in(client, root, "developer")
    secret = client.cookies[web.SESSION_COOKIE]
    clock.move(auth.SESSION_WRITE_EVERY)

    response = client.get(web.HOME_PATH)

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{web.SESSION_COOKIE}={secret};")
    assert "Max-Age=43200" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie


def test_the_refreshed_cookie_is_secure_under_https(root, clock):
    tls = TestClient(create_app(root), base_url="https://testserver", follow_redirects=False)
    web_sign_in(tls, root)
    clock.move(auth.SESSION_WRITE_EVERY)

    assert "Secure" in tls.get(web.HOME_PATH).headers["set-cookie"]


def test_a_route_with_its_own_response_refreshes_the_cookie_too(client, root, clock):
    web_sign_in(client, root)
    clock.move(auth.SESSION_WRITE_EVERY)

    response = client.get(web.PROJECT_PATH.format(slug="no-such-project"))

    assert response.status_code == 404
    assert "Max-Age=43200" in response.headers["set-cookie"]


def test_a_session_used_after_11_hours_lasts_11_hours_past_that_use(client, root, clock):
    web_sign_in(client, root)
    clock.move(11 * 60 * 60)
    assert client.get(web.HOME_PATH).status_code == 200

    clock.move(11 * 60 * 60)

    assert client.get(web.HOME_PATH).status_code == 200


def test_a_session_idle_for_12_hours_goes_to_sign_in(client, root, clock):
    web_sign_in(client, root)
    assert client.get(web.HOME_PATH).status_code == 200

    clock.move(web.SESSION_TTL)
    response = client.get(web.HOME_PATH)

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH
    assert session_of(client, root) is None
