"""The session cookie stays Secure behind a reverse proxy that ends TLS.

``specflo serve`` hands uvicorn ``proxy_headers=False``, so a request's
scheme is the one the socket spoke: plain http behind a proxy that ends
TLS. A request from a peer inside the trusted networks that carries
``X-Forwarded-Proto: https`` counts as https, so every session cookie the
daemon writes on it is Secure: the password sign-in, the proxy sign-in,
the sliding refresh and the sign-out deletion. This holds whether or not
proxy sign-in is on; with it off the trusted list is loopback. The same
header from any other peer counts for nothing, and a direct https request
stays Secure.
"""

import pytest
from fastapi.testclient import TestClient

from specflo import config, daemon
from specflo.daemon import auth, web
from specflo.daemon.app import create_app

from conftest import web_sign_in

HEADER = "X-WEBAUTH-USER"
FORWARDED_HTTPS = {"X-Forwarded-Proto": "https"}
LOOPBACK = ("127.0.0.1", 50000)
MAPPED_LOOPBACK = ("::ffff:127.0.0.1", 50000)
UNTRUSTED = ("10.9.9.9", 50000)


def make_root(tmp_path, text=""):
    """A prepared daemon root whose config also holds ``text``."""
    root = daemon.prepare_root(tmp_path / "daemon")
    path = config.config_path(root)
    path.write_text(path.read_text() + text)
    return root


@pytest.fixture(params=("proxy-off", "proxy-on"))
def root(request, tmp_path):
    """A root with proxy sign-in off and one with it on: the trusted list counts either way."""
    return make_root(tmp_path, "" if request.param == "proxy-off" else "auth_proxy: true\n")


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


def client_for(root, peer=LOOPBACK, headers=FORWARDED_HTTPS, base_url="http://testserver"):
    """A client on a fresh application for ``root``, from ``peer``, sending ``headers`` on every request."""
    return TestClient(
        create_app(root), follow_redirects=False, client=peer, headers=headers, base_url=base_url
    )


def session_cookie(response):
    """The one Set-Cookie header of ``response``, which must be the session cookie's."""
    [cookie] = response.headers.get_list("set-cookie")
    assert cookie.startswith(f"{web.SESSION_COOKIE}=")
    return cookie


def signed_in(client, root):
    """Sign ``client`` in on the form; the sign-in's cookie and the session secret.

    The secret is put back on the client as a plain cookie: a Secure one
    is never sent back over the test's http, which no browser behind the
    proxy would see.
    """
    response = web_sign_in(client, root)
    cookie = session_cookie(response)
    secret = client.cookies[web.SESSION_COOKIE]
    client.cookies.clear()
    client.cookies.set(web.SESSION_COOKIE, secret)
    return cookie, secret


# --- a trusted peer that says https --------------------------------------------


@pytest.mark.parametrize("peer", (LOOPBACK, MAPPED_LOOPBACK), ids=("ipv4", "ipv4-mapped"))
def test_the_password_sign_in_cookie_is_secure(root, peer):
    cookie, _ = signed_in(client_for(root, peer), root)

    assert "Secure" in cookie


def test_the_proxy_sign_in_cookie_is_secure(tmp_path):
    root = make_root(tmp_path, "auth_proxy: true\n")
    auth.add_user(root, "rob", "developer", "correct horse battery staple")
    client = client_for(root, headers={**FORWARDED_HTTPS, HEADER: "rob"})

    response = client.get(web.HOME_PATH)

    assert response.status_code == 200
    assert "Secure" in session_cookie(response)


def test_the_refreshed_cookie_is_secure(root, clock):
    client = client_for(root)
    signed_in(client, root)
    clock.move(auth.SESSION_WRITE_EVERY)

    response = client.get(web.HOME_PATH)

    assert response.status_code == 200
    assert "Secure" in session_cookie(response)


def test_the_sign_out_deletion_is_secure(root):
    client = client_for(root)
    _, secret = signed_in(client, root)

    response = client.post(web.SIGNOUT_PATH, data={"session": secret})

    assert response.status_code == 303
    cookie = session_cookie(response)
    assert "Max-Age=0" in cookie and "Secure" in cookie


def test_the_header_value_is_read_case_insensitively(root):
    client = client_for(root, headers={"X-Forwarded-Proto": "HTTPS"})

    cookie, _ = signed_in(client, root)

    assert "Secure" in cookie


# --- what does not count as https ----------------------------------------------


def test_an_untrusted_peer_saying_https_gets_no_secure_cookie(root, clock):
    client = client_for(root, UNTRUSTED)

    cookie, secret = signed_in(client, root)
    clock.move(auth.SESSION_WRITE_EVERY)
    refreshed = client.get(web.HOME_PATH)
    out = client.post(web.SIGNOUT_PATH, data={"session": secret})

    assert "Secure" not in cookie
    assert "Secure" not in session_cookie(refreshed)
    assert "Secure" not in session_cookie(out)


def test_a_trusted_peer_saying_http_gets_no_secure_cookie(root):
    client = client_for(root, headers={"X-Forwarded-Proto": "http"})

    cookie, _ = signed_in(client, root)

    assert "Secure" not in cookie


def test_an_underscore_spelling_of_the_header_counts_for_nothing(root):
    client = client_for(root, headers={"X_Forwarded_Proto": "https"})

    cookie, _ = signed_in(client, root)

    assert "Secure" not in cookie


# --- a direct https request ---------------------------------------------------


def test_a_direct_https_request_stays_secure(root, clock):
    client = client_for(root, UNTRUSTED, headers=None, base_url="https://testserver")

    response = web_sign_in(client, root)
    secret = client.cookies[web.SESSION_COOKIE]
    clock.move(auth.SESSION_WRITE_EVERY)
    refreshed = client.get(web.HOME_PATH)
    out = client.post(web.SIGNOUT_PATH, data={"session": secret})

    assert "Secure" in session_cookie(response)
    assert "Secure" in session_cookie(refreshed)
    assert "Secure" in session_cookie(out)
