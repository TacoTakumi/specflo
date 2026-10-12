"""A web proxy socket per lease: made at the grant, gone when the lease ends.

A local member leased under a definition with web access reaches the web
through a proxy the daemon serves on a unix socket of that lease's own, in
the daemon root, and resolves names through a DNS forwarder on a second
socket beside it. The grant serves both on the daemon's event loop before
the member starts, and however the lease ends - released, expired, the
member not starting, the daemon stopping - both sockets are removed and a
connect to either former path fails. A lease whose definition has no web
access, or whose member is not local, gets neither.

The pool service runs off the loop, as the daemon's routes run it in a worker
thread; the loop here runs in a thread of its own, as the daemon's does. The
members are the pool tests' real processes. Nothing here leaves this machine:
the one request a test sends asks for a loopback address, which the proxy
refuses before it connects anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import stat
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from specflo.daemon import app as daemon_app
from specflo.errors import SpecfloError
from specflo.pool import runner, service, webproxy
from specflo.pool.cli_admin import pool_dir

from .test_runner import DEFINITION
from .test_webproxy import StubUpstream, dns_answer, dns_query

WEB_DEFINITION = replace(DEFINITION, web=True, web_internal=("127.0.0.1:8731",))


class Made:
    """Makes each lease's proxy as the daemon does, and keeps what it was told.

    The proxy's request log takes the lease, the member and the daemon root;
    what is passed on to the proxy itself is the path and the internal hosts.
    """

    def __init__(self) -> None:
        self.told: list[dict] = []

    def __call__(self, path, internal, **told) -> webproxy.WebProxy:
        self.told.append({"path": Path(path), "internal": tuple(internal), **told})
        return webproxy.WebProxy(path, internal)


class MadeDns:
    """Makes each lease's DNS forwarder over a stub upstream, and keeps what it was told."""

    def __init__(self) -> None:
        self.told: list[dict] = []
        self.upstream = StubUpstream({"research.example": "203.0.113.7"})

    def __call__(self, path, **told) -> webproxy.DnsForwarder:
        self.told.append({"path": Path(path), **told})
        return webproxy.DnsForwarder(path, upstream=self.upstream)


@contextlib.contextmanager
def daemon_loop():
    """An event loop running in a thread of its own, as the daemon's does."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        loop.close()


@pytest.fixture
def web(pool_rig):
    """The daemon's web proxies on the rig's root, served on a loop of their own."""
    made, made_dns = Made(), MadeDns()
    with daemon_loop() as loop:
        proxies = daemon_app.WebProxies(loop, pool_rig.root, make=made, make_dns=made_dns)
        proxies.made, proxies.made_dns = made, made_dns
        try:
            yield proxies
        finally:
            asyncio.run_coroutine_threadsafe(proxies.aclose(), loop).result(10)


def web_service(pool_rig, web, *members, definition=WEB_DEFINITION):
    config = pool_rig.config(*(members or (pool_rig.local_member(),)))
    svc = pool_rig.service(replace(config, definitions=(definition,)))
    svc.web = web
    return svc


def asked_for_loopback(path: Path) -> bytes:
    """What the proxy at *path* answers a CONNECT to a loopback address with."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(path))
        client.sendall(b"CONNECT 127.0.0.1:9 HTTP/1.1\r\nHost: 127.0.0.1:9\r\n\r\n")
        return client.recv(4096)


def asked_for_a_name(path: Path) -> bytes:
    """What the DNS forwarder at *path* answers a query for a name with."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(path))
        client.sendall(dns_query("research.example"))
        client.shutdown(socket.SHUT_WR)
        answer = b""
        while chunk := client.recv(4096):
            answer += chunk
        return answer


def assert_gone(path: Path) -> None:
    assert not os.path.lexists(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client, pytest.raises(OSError):
        client.connect(str(path))


def assert_both_gone(web, lease_id: str) -> None:
    """Neither the proxy socket nor the DNS socket of the lease *lease_id* is there."""
    assert_gone(web.path(lease_id))
    assert_gone(web.dns_path(lease_id))


def test_a_web_lease_gets_a_socket_of_its_own_in_the_daemon_root(pool_rig, web):
    svc = web_service(pool_rig, web)

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    path = service.web_socket_path(pool_rig.root, grant.lease_id)
    assert path == pool_rig.root / "web" / "lease-1.sock"
    assert web.path(grant.lease_id) == path
    assert stat.S_ISSOCK(os.lstat(path).st_mode)
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    # served on the daemon's loop: this thread waits on the answer, and the
    # proxy refuses a loopback address before it connects anywhere
    assert asked_for_loopback(path).startswith(b"HTTP/1.1 403")
    assert web.made.told == [{
        "path": path, "internal": ("127.0.0.1:8731",),
        "lease": "lease-1", "member": "local-1", "root": pool_rig.root,
    }]
    svc.end_lease(grant.lease_id, "released")


def test_a_web_lease_gets_a_dns_socket_beside_its_proxy_socket(pool_rig, web):
    svc = web_service(pool_rig, web)

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    path = service.dns_socket_path(pool_rig.root, grant.lease_id)
    assert path == pool_rig.root / "web" / "lease-1.dns"
    assert web.dns_path(grant.lease_id) == path
    assert stat.S_ISSOCK(os.lstat(path).st_mode)
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    # served on the daemon's loop, through the upstream it was made with
    query = dns_query("research.example")
    assert asked_for_a_name(path) == dns_answer(query, "203.0.113.7")
    assert web.made_dns.upstream.queries == [query]
    assert web.made_dns.told == [{
        "path": path, "lease": "lease-1", "member": "local-1", "root": pool_rig.root,
    }]
    svc.end_lease(grant.lease_id, "released")
    assert_gone(path)


def test_ending_the_lease_removes_the_socket_and_a_connect_fails(pool_rig, web):
    svc = web_service(pool_rig, web)
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    path = web.path(grant.lease_id)
    assert path.exists()

    svc.end_lease(grant.lease_id, "released")

    assert_both_gone(web, grant.lease_id)
    # an ending that changes nothing touches nothing
    assert svc.end_lease(grant.lease_id, "released").state == "released"
    assert_both_gone(web, grant.lease_id)


def test_an_expired_lease_has_its_socket_removed(pool_rig, web):
    # a member's host stamps its activity with the real time
    pool_rig.clock.now = datetime.now(timezone.utc)
    svc = web_service(pool_rig, web)
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    path = web.path(grant.lease_id)
    assert path.exists()

    pool_rig.clock.advance(minutes=11)
    ended = svc.expire_due()

    assert [e.lease_id for e in ended] == [grant.lease_id]
    assert_both_gone(web, grant.lease_id)


def test_a_member_that_does_not_start_leaves_no_socket(pool_rig, web, monkeypatch):
    svc = web_service(pool_rig, web)
    seen = []

    def no_start(*args, **kwargs):
        # both sockets are there when the member starts, for its sandbox to bind
        seen.append(os.path.lexists(web.path("lease-1")))
        seen.append(os.path.lexists(web.dns_path("lease-1")))
        raise runner.RunnerError("the member did not come up")

    monkeypatch.setattr(runner, "start", no_start)

    with pytest.raises(runner.RunnerError):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert seen == [True, True]
    assert_both_gone(web, "lease-1")
    with pool_rig.store() as store:
        assert store.get_lease("lease-1").state == "released"


def test_a_lease_without_web_access_gets_no_socket(pool_rig, web):
    svc = web_service(pool_rig, web, definition=DEFINITION)

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert not os.path.lexists(web.path(grant.lease_id))
    assert not os.path.lexists(web.dns_path(grant.lease_id))
    assert web.made.told == [] and web.made_dns.told == []
    svc.end_lease(grant.lease_id, "released")


def test_a_hosted_member_under_a_web_definition_gets_no_socket(pool_rig, web):
    svc = web_service(pool_rig, web, pool_rig.hosted_member())

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert not os.path.lexists(web.path(grant.lease_id))
    assert not os.path.lexists(web.dns_path(grant.lease_id))
    assert web.made.told == [] and web.made_dns.told == []
    svc.end_lease(grant.lease_id, "released")


def test_a_web_lease_on_a_daemon_with_no_proxy_is_refused(pool_rig):
    svc = pool_rig.service(
        replace(pool_rig.config(pool_rig.local_member()), definitions=(WEB_DEFINITION,))
    )

    with pytest.raises(SpecfloError, match="web"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert pool_rig.pane_names() == []
    with pool_rig.store() as store:
        assert store.get_lease("lease-1").state == "released"


def test_stopping_the_daemon_removes_every_socket_and_serves_no_more(pool_rig, web):
    svc = web_service(pool_rig, web)
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    path = web.path(grant.lease_id)
    assert path.exists()

    asyncio.run_coroutine_threadsafe(web.aclose(), web.loop).result(10)

    assert_both_gone(web, grant.lease_id)
    # the lease still ends as ever, and a new web lease is refused
    svc.end_lease(grant.lease_id, "released")
    with pytest.raises(SpecfloError, match="stopping"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert_gone(web.path("lease-2"))


def test_the_daemon_serves_the_web_proxies_of_its_pool_on_its_loop(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    pool_dir(root).mkdir()
    # a socket left by a daemon that died is not served by anyone
    left = root / "web" / "lease-left.sock"
    left_dns = root / "web" / "lease-left.dns"
    left.parent.mkdir()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale, \
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale_dns:
        stale.bind(str(left))
        stale_dns.bind(str(left_dns))
    application = daemon_app.create_app(root)

    with TestClient(application):
        web = application.state.web
        assert isinstance(web, daemon_app.WebProxies)
        assert web.root == root
        assert not os.path.lexists(left)
        assert not os.path.lexists(left_dns)

    with pytest.raises(SpecfloError, match="stopping"):
        web.open("lease-1", "local-1", ())
