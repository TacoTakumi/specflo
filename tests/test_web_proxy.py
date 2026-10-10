"""Proxy sign-in: a reverse proxy names the user, and the browser skips the form.

With ``auth_proxy`` on, a page asked for by a peer inside the trusted list,
carrying the configured header with an existing user's name, signs that
user in without the form: a session opened with the method 'proxy', its
cookie on the response. The peer is the socket peer only, so an untrusted
peer gets the form whatever X-Forwarded-For says, and only the header's own
name counts, not an underscore spelling of it. The header wins over a live
session of another user, which ends; a name no user has gets a 403 that
says how to add one, never the form. The role is the user row's: no role
or group header is read. Proxy sign-in checks no password, so the lockout
does not stop it. The header never unlocks the API, which reads only the
Authorization header.

That ``specflo serve`` hands uvicorn ``proxy_headers=False``, so a request's
peer is the socket peer, is tested in test_daemon_proxy_config.py.
"""

import re

import pytest
from fastapi.testclient import TestClient

from specflo import config, daemon
from specflo.daemon import auth, routes, web
from specflo.daemon.app import create_app
from specflo.daemon.store import open_store

PASSWORD = "correct horse battery staple"
HEADER = "X-WEBAUTH-USER"
LOOPBACK = ("127.0.0.1", 50000)
UNTRUSTED = ("10.9.9.9", 50000)


def make_root(tmp_path, text="auth_proxy: true\n"):
    """A prepared daemon root whose config also holds ``text``, with rob, a developer."""
    root = daemon.prepare_root(tmp_path / "daemon")
    path = config.config_path(root)
    path.write_text(path.read_text() + text)
    auth.add_user(root, "rob", "developer", PASSWORD)
    return root


@pytest.fixture
def root(tmp_path):
    return make_root(tmp_path)


def client_for(root, peer=LOOPBACK):
    """A client on a fresh application for ``root`` whose requests come from ``peer``."""
    return TestClient(create_app(root), follow_redirects=False, client=peer)


def session_of(client, root):
    """The session row the client's cookie stands for, or None."""
    secret = client.cookies.get(web.SESSION_COOKIE)
    if not secret:
        return None
    with open_store(root) as store:
        return store.get_session(auth.hash_token(secret))


def sessions(root):
    """How many session rows the store holds."""
    with open_store(root) as store:
        return store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def assert_sent_to_sign_in(response):
    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH
    assert "set-cookie" not in response.headers


# --- signed in through the proxy -----------------------------------------------


def test_with_the_proxy_off_the_header_signs_no_one_in(tmp_path):
    root = make_root(tmp_path, text="")
    client = client_for(root)

    assert_sent_to_sign_in(client.get(web.HOME_PATH, headers={HEADER: "rob"}))
    assert sessions(root) == 0


def test_a_trusted_peer_naming_a_user_gets_the_page_as_that_user(root):
    client = client_for(root)

    response = client.get(web.HOME_PATH, headers={HEADER: " Rob "})

    assert response.status_code == 200
    assert "signed in as <strong>developer</strong>" in response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{web.SESSION_COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=43200" in cookie
    session = session_of(client, root)
    assert session.user == "rob"
    assert session.method == "proxy"


def test_the_first_page_echoes_the_session_it_opened(root):
    client = client_for(root)

    response = client.get(web.HOME_PATH, headers={HEADER: "rob"})

    secret = client.cookies[web.SESSION_COOKIE]
    echoed = re.findall(r'name="session" value="([^"]*)"', response.text)
    assert echoed and set(echoed) == {secret}
    # The sign-out form it carries works: the echo matches the new cookie.
    out = client.post(web.SIGNOUT_PATH, data={"session": secret})
    assert out.status_code == 303 and out.headers["location"] == web.SIGNIN_PATH
    assert sessions(root) == 0


def test_a_proxy_session_gets_no_change_password_link(root):
    client = client_for(root)

    response = client.get(web.HOME_PATH, headers={HEADER: "rob"})

    assert response.status_code == 200
    assert web.PASSWORD_PATH not in response.text


def test_the_live_proxy_session_is_used_on_later_requests(root):
    client = client_for(root)
    client.get(web.HOME_PATH, headers={HEADER: "rob"})

    again = client.get(web.HOME_PATH, headers={HEADER: "rob"})

    assert again.status_code == 200
    assert sessions(root) == 1


def test_the_sign_in_page_with_the_header_goes_home_signed_in(root):
    client = client_for(root)

    response = client.get(web.SIGNIN_PATH, headers={HEADER: "rob"})

    assert response.status_code == 303
    assert response.headers["location"] == web.HOME_PATH
    assert session_of(client, root).method == "proxy"
    assert client.get(web.HOME_PATH).status_code == 200


def test_a_second_look_in_the_same_request_opens_no_other_session(root):
    # The error page looks up the session again after the route opened one.
    from fastapi import Depends

    app = create_app(root)

    @app.get("/twice", include_in_schema=False)
    def twice(request: web.Request, user=Depends(web.current_user)):
        return {"again": web.current_user(request).name}

    client = TestClient(app, follow_redirects=False, client=LOOPBACK)
    response = client.get("/twice", headers={HEADER: "rob"})

    assert response.json() == {"again": "rob"}
    assert sessions(root) == 1


def test_a_route_with_its_own_response_sets_the_cookie_too(root):
    client = client_for(root)

    response = client.get(web.TOKENS_PATH, headers={HEADER: "rob"})

    assert response.status_code == 200
    assert response.headers["set-cookie"].startswith(f"{web.SESSION_COOKIE}=")


def test_a_custom_header_is_read_and_the_default_is_not(tmp_path):
    root = make_root(tmp_path, text="auth_proxy: true\nauth_proxy_header: X-Forwarded-User\n")
    client = client_for(root)

    assert_sent_to_sign_in(client.get(web.HOME_PATH, headers={HEADER: "rob"}))
    response = client.get(web.HOME_PATH, headers={"x-forwarded-user": "rob"})

    assert response.status_code == 200
    assert session_of(client, root).user == "rob"


def test_ipv6_loopback_is_trusted_by_default(root):
    client = client_for(root, peer=("::1", 50000))

    assert client.get(web.HOME_PATH, headers={HEADER: "rob"}).status_code == 200


def test_an_ipv4_peer_seen_through_a_dual_stack_socket_is_trusted(root):
    client = client_for(root, peer=("::ffff:127.0.0.1", 50000))

    assert client.get(web.HOME_PATH, headers={HEADER: "rob"}).status_code == 200


def test_a_listed_network_trusts_every_peer_inside_it(tmp_path):
    root = make_root(tmp_path, text="auth_proxy: true\nauth_trusted_proxies: ['10.0.0.0/24']\n")

    inside = client_for(root, peer=("10.0.0.7", 50000))
    outside = client_for(root, peer=("10.0.1.7", 50000))

    assert inside.get(web.HOME_PATH, headers={HEADER: "rob"}).status_code == 200
    assert_sent_to_sign_in(outside.get(web.HOME_PATH, headers={HEADER: "rob"}))


def test_a_proxy_only_user_signs_in_through_the_proxy(root):
    auth.add_user(root, "ann", "requester", None)
    client = client_for(root)

    response = client.get(web.HOME_PATH, headers={HEADER: "ann"})

    assert response.status_code == 200
    assert "signed in as <strong>requester</strong>" in response.text
    assert session_of(client, root).user == "ann"


def test_a_locked_out_user_still_signs_in_through_the_proxy(root):
    form = client_for(root)
    for _ in range(auth.SIGNIN_LOCK_FAILURES):
        form.post(web.SIGNIN_PATH, data={"name": "rob", "password": "not it"})
    locked = form.post(web.SIGNIN_PATH, data={"name": "rob", "password": PASSWORD})
    assert locked.status_code == 429

    client = client_for(root)
    response = client.get(web.HOME_PATH, headers={HEADER: "rob"})

    assert response.status_code == 200
    assert session_of(client, root).method == "proxy"


def test_proxy_sign_in_never_checks_a_password(root, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("proxy sign-in checked a password")

    monkeypatch.setattr(web, "check_password", refuse)
    monkeypatch.setattr(auth, "check_password", refuse)
    client = client_for(root)

    assert client.get(web.HOME_PATH, headers={HEADER: "rob"}).status_code == 200


# --- not signed in -------------------------------------------------------------


def test_an_untrusted_peer_gets_the_form(root):
    client = client_for(root, peer=UNTRUSTED)

    assert_sent_to_sign_in(client.get(web.HOME_PATH, headers={HEADER: "rob"}))
    form = client.get(web.SIGNIN_PATH, headers={HEADER: "rob"})
    assert form.status_code == 200 and 'name="password"' in form.text
    assert sessions(root) == 0


@pytest.mark.parametrize("forwarded", [
    {"X-Forwarded-For": "127.0.0.1"},
    {"Forwarded": "for=127.0.0.1"},
    {"X-Real-IP": "127.0.0.1"},
])
def test_forwarding_headers_do_not_make_a_peer_trusted(root, forwarded):
    client = client_for(root, peer=UNTRUSTED)

    response = client.get(web.HOME_PATH, headers={HEADER: "rob", **forwarded})

    assert_sent_to_sign_in(response)
    assert sessions(root) == 0


def test_a_peer_with_no_address_is_untrusted(root):
    # The TestClient's default peer is the host name 'testclient'.
    client = TestClient(create_app(root), follow_redirects=False)

    assert_sent_to_sign_in(client.get(web.HOME_PATH, headers={HEADER: "rob"}))


def test_an_underscore_spelling_of_the_header_signs_no_one_in(root):
    client = client_for(root)

    assert_sent_to_sign_in(client.get(web.HOME_PATH, headers={"X_WEBAUTH_USER": "rob"}))
    assert sessions(root) == 0


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_header_signs_no_one_in(root, value):
    client = client_for(root)

    assert_sent_to_sign_in(client.get(web.HOME_PATH, headers={HEADER: value}))


def test_the_header_twice_signs_no_one_in(root):
    client = client_for(root)

    response = client.get(web.HOME_PATH, headers=[(HEADER, "rob"), (HEADER, "rob")])

    assert_sent_to_sign_in(response)


def assert_refused_unknown(response, name):
    assert response.status_code == 403
    assert "specflo serve user add" in response.text
    assert name in response.text
    assert 'name="password"' not in response.text
    assert "set-cookie" not in response.headers


def users(root):
    with open_store(root) as store:
        return [user.name for user in store.list_users()]


@pytest.mark.parametrize("path", [web.HOME_PATH, web.SIGNIN_PATH, web.TOKENS_PATH])
def test_an_unknown_user_is_refused_and_not_created(root, path):
    client = client_for(root)

    response = client.get(path, headers={HEADER: "Nobody"})

    assert_refused_unknown(response, "specflo serve user add nobody")
    assert users(root) == ["rob"]
    assert sessions(root) == 0


def test_the_unknown_name_is_escaped_on_the_refusal(root):
    client = client_for(root)

    response = client.get(web.HOME_PATH, headers={HEADER: "<b>x</b>"})

    assert response.status_code == 403
    assert "<b>x</b>" not in response.text
    assert "&lt;b&gt;x&lt;/b&gt;" in response.text


def test_an_unknown_user_is_refused_beside_a_live_session_which_is_left_alone(root):
    client = client_for(root)
    client.post(web.SIGNIN_PATH, data={"name": "rob", "password": PASSWORD})
    before = session_of(client, root)

    response = client.get(web.HOME_PATH, headers={HEADER: "nobody"})

    assert_refused_unknown(response, "nobody")
    assert session_of(client, root) == before
    assert sessions(root) == 1


def test_the_header_naming_another_user_replaces_the_live_session(root):
    auth.add_user(root, "ann", "requester", PASSWORD)
    client = client_for(root)
    client.post(web.SIGNIN_PATH, data={"name": "rob", "password": PASSWORD})
    old = client.cookies[web.SESSION_COOKIE]

    response = client.get(web.HOME_PATH, headers={HEADER: "ann"})

    assert response.status_code == 200
    assert "signed in as <strong>requester</strong>" in response.text
    assert response.headers["set-cookie"].startswith(f"{web.SESSION_COOKIE}=")
    with open_store(root) as store:
        assert store.get_session(auth.hash_token(old)) is None
    session = session_of(client, root)
    assert session.user == "ann" and session.method == "proxy"
    assert sessions(root) == 1


def test_the_header_naming_the_session_user_keeps_a_password_session(root):
    client = client_for(root)
    client.post(web.SIGNIN_PATH, data={"name": "rob", "password": PASSWORD})
    secret = client.cookies[web.SESSION_COOKIE]

    response = client.get(web.HOME_PATH, headers={HEADER: "rob"})

    assert response.status_code == 200
    assert client.cookies[web.SESSION_COOKIE] == secret
    assert session_of(client, root).method == "password"
    assert sessions(root) == 1


def test_role_and_group_headers_are_never_read(root):
    auth.add_user(root, "ann", "requester", None)
    client = client_for(root)
    headers = {
        HEADER: "ann", "X-WEBAUTH-ROLE": "developer", "X-WEBAUTH-GROUPS": "developer",
    }

    response = client.get(web.DEFINITIONS_PATH, headers=headers)

    assert response.status_code == 403
    assert "requester" in response.text
    assert session_of(client, root).user == "ann"


# --- the API -------------------------------------------------------------------


def test_the_header_never_authenticates_an_api_call(root):
    client = client_for(root)

    assert client.get(routes.WHOAMI_PATH, headers={HEADER: "rob"}).status_code == 401
    assert sessions(root) == 0
