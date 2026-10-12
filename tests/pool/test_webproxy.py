"""The web proxy: what it forwards, what it refuses, and where it connects.

A web member reaches the internet through a proxy the daemon serves on a unix
socket. It forwards a CONNECT and a plain HTTP request with an absolute URI to
a public host, and answers 403, before any connection is opened, for a host
whose address is not public, unless the definition lists that host and port
as internal. It resolves a name once and connects to the very address it
checked, so a name that resolves to a private address is refused, and a name
that answers differently the second time is never asked a second time.

Nothing here leaves this machine. The resolver is a stub that maps names to
the addresses a test gives it, and the dialer is a stub that records the
address the proxy asked for and connects to an upstream served in this
process. The one test that dials for real lists that upstream's own loopback
address as internal.
"""

from __future__ import annotations

import ast
import asyncio
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Self

import pytest

from specflo.errors import SpecfloError
from specflo.pool import webproxy

PUBLIC = "93.184.215.14"
PUBLIC_NAME = "public.example"

# One address of each range the proxy refuses, by what makes it so.
REFUSED_ADDRESSES = {
    "loopback": "127.0.0.1",
    "loopback, not the first": "127.8.9.10",
    "rfc1918 10/8": "10.0.0.5",
    "rfc1918 172.16/12": "172.31.1.21",
    "rfc1918 192.168/16": "192.168.1.1",
    "link-local": "169.254.169.254",
    "shared cgnat": "100.64.0.1",
    "unspecified": "0.0.0.0",
    "this network": "0.1.2.3",
    "multicast": "224.0.0.1",
    "broadcast": "255.255.255.255",
    "reserved": "240.0.0.1",
    "benchmarking": "198.18.0.1",
    "ipv6 loopback": "::1",
    "ipv6 unspecified": "::",
    "unique local fc": "fc00::1",
    "unique local fd": "fd12:3456::1",
    "ipv6 link-local": "fe80::1",
    "ipv6 multicast": "ff02::1",
    "ipv4-mapped private": "::ffff:10.0.0.5",
    "ipv4-mapped loopback": "::ffff:127.0.0.1",
    "ipv4-compatible private": "::a00:5",
    "6to4 of a private address": "2002:a00:5::1",
    "nat64 of a private address": "64:ff9b::a00:5",
    "documentation": "2001:db8::1",
}


@pytest.fixture
def short_dir():
    """A directory with a path short enough to bind a unix socket in."""
    made = tempfile.mkdtemp(dir="/tmp", prefix="specflo-wp-")
    try:
        yield Path(made)
    finally:
        shutil.rmtree(made, ignore_errors=True)


@pytest.fixture
def path(short_dir: Path) -> Path:
    return short_dir / "web.sock"


class StubResolver:
    """Names mapped to the addresses a test gives; every lookup is recorded."""

    def __init__(self, names: dict[str, list[str]] | None = None, *, error: Exception | None = None):
        self.names = names or {}
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        if self.error is not None:
            raise self.error
        return list(self.names.get(host, []))


class StubDialer:
    """Records the address the proxy dials and connects to the test's upstream instead."""

    def __init__(self, upstream: tuple[str, int] | None = None):
        self.upstream = upstream
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int):
        self.calls.append((host, port))
        if self.upstream is None:
            raise ConnectionRefusedError("no upstream in this test")
        return await asyncio.open_connection(*self.upstream)


class Upstream:
    """An HTTP server on loopback that answers each request with the head it was sent."""

    def __init__(self) -> None:
        self.heads: list[str] = []
        self.bodies: list[bytes] = []
        self.server: asyncio.base_events.Server | None = None

    @property
    def address(self) -> tuple[str, int]:
        assert self.server is not None
        return self.server.sockets[0].getsockname()[:2]

    async def __aenter__(self) -> Self:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            self.heads.append(head)
            length = 0
            for line in head.split("\r\n")[1:]:
                name, _, value = line.partition(":")
                if name.lower() == "content-length":
                    length = int(value.strip())
            body = await reader.readexactly(length) if length else b""
            self.bodies.append(body)
            answer = head.encode("latin-1") + body
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                + f"Content-Length: {len(answer)}\r\nConnection: close\r\n\r\n".encode()
                + answer
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def exchange(path: Path, request: bytes) -> bytes:
    """Send *request* to the proxy at *path* and read its answer to the end."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 10)
    finally:
        writer.close()


async def answer_head(path: Path, request: bytes) -> bytes:
    """Send *request* to the proxy at *path* and read the head of its answer only,
    which is all a CONNECT that is let through answers before the tunnel."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    finally:
        writer.close()


def status(answer: bytes) -> int:
    return int(answer.split(b" ", 2)[1])


def proxy(path: Path, internal: Iterable[str] = (), **stubs) -> webproxy.WebProxy:
    return webproxy.WebProxy(path, internal, **stubs)


def test_a_connect_to_a_public_host_is_tunnelled_to_the_address_it_resolved_to(path):
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC]})

    async def run() -> None:
        async with Upstream() as upstream:
            dialer = StubDialer(upstream.address)
            async with proxy(path, resolver=resolver, dialer=dialer):
                reader, writer = await asyncio.open_unix_connection(str(path))
                try:
                    writer.write(b"CONNECT public.example:443 HTTP/1.1\r\nHost: public.example:443\r\n\r\n")
                    await writer.drain()
                    established = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                    assert status(established) == 200
                    # what goes through the tunnel reaches the upstream as it is sent
                    writer.write(b"GET /through HTTP/1.1\r\nHost: public.example\r\n\r\n")
                    await writer.drain()
                    answer = await asyncio.wait_for(reader.read(), 10)
                finally:
                    writer.close()
            assert dialer.calls == [(PUBLIC, 443)]
            assert resolver.calls == [(PUBLIC_NAME, 443)]
            assert status(answer) == 200
            assert upstream.heads[0].startswith("GET /through HTTP/1.1\r\n")

    asyncio.run(run())


def test_an_absolute_uri_get_is_forwarded_in_origin_form_without_proxy_headers(path):
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC]})

    async def run() -> None:
        async with Upstream() as upstream:
            dialer = StubDialer(upstream.address)
            async with proxy(path, resolver=resolver, dialer=dialer):
                answer = await exchange(path, (
                    b"GET http://public.example/page?q=1 HTTP/1.1\r\n"
                    b"Host: public.example\r\n"
                    b"Proxy-Connection: keep-alive\r\n"
                    b"Proxy-Authorization: Basic c2VjcmV0\r\n"
                    b"Accept: text/plain\r\n\r\n"
                ))
            assert dialer.calls == [(PUBLIC, 80)]
            assert status(answer) == 200
            head = upstream.heads[0]
            lines = head.split("\r\n")
            assert lines[0] == "GET /page?q=1 HTTP/1.1"
            names = [line.partition(":")[0].lower() for line in lines[1:] if line]
            assert "proxy-connection" not in names
            assert "proxy-authorization" not in names
            assert "Host: public.example" in lines
            assert "Accept: text/plain" in lines
            assert "Connection: close" in lines

    asyncio.run(run())


def test_a_post_body_is_forwarded_whole(path):
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC]})

    async def run() -> None:
        async with Upstream() as upstream:
            dialer = StubDialer(upstream.address)
            async with proxy(path, resolver=resolver, dialer=dialer):
                answer = await exchange(path, (
                    b"POST http://public.example:8080/form HTTP/1.1\r\n"
                    b"Host: public.example:8080\r\nContent-Length: 11\r\n\r\nhello there"
                ))
            assert dialer.calls == [(PUBLIC, 8080)]
            assert status(answer) == 200
            assert upstream.bodies == [b"hello there"]
            assert answer.endswith(b"hello there")

    asyncio.run(run())


@pytest.mark.parametrize("address", REFUSED_ADDRESSES.values(), ids=REFUSED_ADDRESSES.keys())
@pytest.mark.parametrize("form", ["connect literal", "connect name", "absolute uri name"])
def test_a_host_that_is_not_public_is_refused_with_no_connection(path, address, form):
    resolver = StubResolver({"named.example": [address]})
    dialer = StubDialer()
    literal = f"[{address}]" if ":" in address else address
    request = {
        "connect literal": f"CONNECT {literal}:443 HTTP/1.1\r\n\r\n",
        "connect name": "CONNECT named.example:443 HTTP/1.1\r\n\r\n",
        "absolute uri name": "GET http://named.example/ HTTP/1.1\r\nHost: named.example\r\n\r\n",
    }[form].encode()

    async def run() -> bytes:
        async with proxy(path, resolver=resolver, dialer=dialer):
            return await exchange(path, request)

    answer = asyncio.run(run())
    assert status(answer) == webproxy.REFUSED
    assert dialer.calls == []
    if form == "connect literal":
        assert resolver.calls == []


def test_a_public_looking_name_that_resolves_to_a_private_address_is_refused(path):
    resolver = StubResolver({"news.example": ["10.0.0.5"]})
    dialer = StubDialer()

    async def run() -> bytes:
        async with proxy(path, resolver=resolver, dialer=dialer):
            return await exchange(path, b"CONNECT news.example:443 HTTP/1.1\r\n\r\n")

    answer = asyncio.run(run())
    assert status(answer) == 403
    assert b"10.0.0.5" in answer
    assert dialer.calls == []


def test_a_name_with_one_private_address_among_public_ones_is_refused(path):
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC, "10.0.0.5"]})
    dialer = StubDialer()

    async def run() -> bytes:
        async with proxy(path, resolver=resolver, dialer=dialer):
            return await exchange(path, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")

    assert status(asyncio.run(run())) == 403
    assert dialer.calls == []


def test_a_name_is_resolved_once_and_the_checked_address_is_the_one_dialled(path):
    class Rebinding(StubResolver):
        """Public on the first lookup, private on every one after it."""

        async def __call__(self, host: str, port: int) -> list[str]:
            self.calls.append((host, port))
            return [PUBLIC] if len(self.calls) == 1 else ["127.0.0.1"]

    resolver = Rebinding()

    async def run() -> None:
        async with Upstream() as upstream:
            dialer = StubDialer(upstream.address)
            async with proxy(path, resolver=resolver, dialer=dialer):
                answer = await exchange(path, b"GET http://rebind.example/ HTTP/1.1\r\n\r\n")
            assert status(answer) == 200
            assert resolver.calls == [("rebind.example", 80)]
            assert dialer.calls == [(PUBLIC, 80)]

    asyncio.run(run())


def test_a_listed_internal_host_and_port_is_dialled_for_real(path):
    async def run() -> None:
        async with Upstream() as upstream:
            host, port = upstream.address
            async with proxy(path, [f"{host}:{port}"], resolver=StubResolver()):
                got = await exchange(path, f"GET http://{host}:{port}/inside HTTP/1.1\r\n\r\n".encode())
                reader, writer = await asyncio.open_unix_connection(str(path))
                try:
                    writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\n\r\n".encode())
                    await writer.drain()
                    assert status(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)) == 200
                    writer.write(b"GET /tunnel HTTP/1.1\r\n\r\n")
                    await writer.drain()
                    tunnelled = await asyncio.wait_for(reader.read(), 10)
                finally:
                    writer.close()
            assert status(got) == 200
            assert status(tunnelled) == 200
            assert [h.split("\r\n")[0] for h in upstream.heads] == [
                "GET /inside HTTP/1.1", "GET /tunnel HTTP/1.1",
            ]

    asyncio.run(run())


def test_a_listed_internal_name_is_allowed_on_its_port_only(path):
    resolver = StubResolver({"intranet.lan": ["127.0.0.1"]})

    async def run() -> tuple[bytes, bytes]:
        async with Upstream() as upstream:
            dialer = StubDialer(upstream.address)
            async with proxy(path, ["Intranet.LAN:8731"], resolver=resolver, dialer=dialer):
                listed = await answer_head(path, b"CONNECT intranet.lan:8731 HTTP/1.1\r\n\r\n")
                other = await answer_head(path, b"CONNECT intranet.lan:8732 HTTP/1.1\r\n\r\n")
            assert dialer.calls == [("127.0.0.1", 8731)]
            return listed, other

    listed, other = asyncio.run(run())
    assert status(listed) == 200
    assert status(other) == 403


def test_a_name_that_resolves_to_a_listed_internal_address_is_allowed_on_that_port(path):
    resolver = StubResolver({"intranet.lan": ["127.0.0.1"]})

    async def run() -> tuple[bytes, bytes]:
        async with Upstream() as upstream:
            dialer = StubDialer(upstream.address)
            async with proxy(path, ["127.0.0.1:8731"], resolver=resolver, dialer=dialer):
                listed = await answer_head(path, b"CONNECT intranet.lan:8731 HTTP/1.1\r\n\r\n")
                other = await answer_head(path, b"CONNECT intranet.lan:22 HTTP/1.1\r\n\r\n")
            assert dialer.calls == [("127.0.0.1", 8731)]
            return listed, other

    listed, other = asyncio.run(run())
    assert status(listed) == 200
    assert status(other) == 403


def test_the_system_resolver_is_checked_like_any_other(path):
    # localhost resolves to loopback from the hosts file, with nothing asked of
    # the network, and loopback is refused
    async def run() -> bytes:
        async with proxy(path):
            return await exchange(path, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")

    answer = asyncio.run(run())
    assert status(answer) == 403
    assert b"127.0.0.1" in answer or b"::1" in answer


def test_an_internal_entry_that_is_not_host_and_port_is_refused_by_name(path):
    with pytest.raises(SpecfloError, match="intranet.lan"):
        webproxy.WebProxy(path, ["intranet.lan"])


@pytest.mark.parametrize("resolver", [
    StubResolver(error=OSError("no such name")),
    StubResolver({}),
    StubResolver({"odd.example": ["not an address"]}),
], ids=["lookup fails", "no address", "not an address"])
def test_a_name_that_does_not_resolve_cleanly_fails_closed(path, resolver):
    dialer = StubDialer()

    async def run() -> bytes:
        async with proxy(path, resolver=resolver, dialer=dialer):
            return await exchange(path, b"CONNECT odd.example:443 HTTP/1.1\r\n\r\n")

    assert status(asyncio.run(run())) in (403, 502)
    assert dialer.calls == []


def test_an_upstream_that_cannot_be_reached_is_answered_502(path):
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC]})
    dialer = StubDialer()

    async def run() -> bytes:
        async with proxy(path, resolver=resolver, dialer=dialer):
            return await exchange(path, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")

    assert status(asyncio.run(run())) == 502
    assert dialer.calls == [(PUBLIC, 443)]


@pytest.mark.parametrize("request_bytes", [
    b"GET / HTTP/1.1\r\nHost: public.example\r\n\r\n",
    b"GET https://public.example/ HTTP/1.1\r\n\r\n",
    b"GET ftp://public.example/ HTTP/1.1\r\n\r\n",
    b"GET http://user@public.example/ HTTP/1.1\r\n\r\n",
    b"CONNECT public.example HTTP/1.1\r\n\r\n",
    b"CONNECT public.example:0 HTTP/1.1\r\n\r\n",
    b"CONNECT public.example:99999 HTTP/1.1\r\n\r\n",
    b"CONNECT :443 HTTP/1.1\r\n\r\n",
    b"GET http://public.example/ HTTP/2.0\r\n\r\n",
    b"GET http://public.example/\r\n\r\n",
    b"GET http://public.example/ HTTP/1.1\r\nno colon here\r\n\r\n",
    b"GET http://public.example/ HTTP/1.1\r\nX-A: 1\nX-B: 2\r\n\r\n",
    b"POST http://public.example/ HTTP/1.1\r\nContent-Length: 3\r\nContent-Length: 4\r\n\r\nabc",
    b"POST http://public.example/ HTTP/1.1\r\nContent-Length: -1\r\n\r\n",
    b"POST http://public.example/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
], ids=[
    "origin form", "https uri", "ftp uri", "userinfo", "connect without port", "port zero",
    "port too large", "empty host", "http 2", "no version", "header without colon",
    "bare line feed", "two lengths", "negative length", "chunked body",
])
def test_a_request_the_proxy_cannot_read_safely_is_refused_with_no_connection(path, request_bytes):
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC]})
    dialer = StubDialer()

    async def run() -> bytes:
        async with proxy(path, resolver=resolver, dialer=dialer):
            return await exchange(path, request_bytes)

    answer = asyncio.run(run())
    assert 400 <= status(answer) < 500 or status(answer) == 501
    assert dialer.calls == []
    assert resolver.calls == []


def test_a_head_larger_than_the_limit_is_refused(path):
    dialer = StubDialer()
    request = b"GET http://public.example/ HTTP/1.1\r\nX-Big: " + b"a" * (128 * 1024) + b"\r\n\r\n"

    async def run() -> bytes:
        async with proxy(path, resolver=StubResolver(), dialer=dialer):
            return await exchange(path, request)

    assert status(asyncio.run(run())) == 431
    assert dialer.calls == []


def test_the_socket_is_owner_only_while_served_and_gone_after(path):
    async def run() -> None:
        served = proxy(path, resolver=StubResolver(), dialer=StubDialer())
        assert served.path == path
        await served.start()
        try:
            mode = os.stat(path).st_mode
            assert stat.S_ISSOCK(mode)
            assert stat.S_IMODE(mode) == 0o600
        finally:
            await served.stop()
        assert not path.exists()
        await served.stop()  # a second stop is a no-op

    asyncio.run(run())


def test_stop_ends_an_open_tunnel(path):
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC]})

    async def run() -> None:
        async with Upstream() as upstream:
            served = proxy(path, resolver=resolver, dialer=StubDialer(upstream.address))
            await served.start()
            reader, writer = await asyncio.open_unix_connection(str(path))
            try:
                writer.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
                await writer.drain()
                assert status(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)) == 200
                await asyncio.wait_for(served.stop(), 10)
                assert await asyncio.wait_for(reader.read(), 10) == b""
            finally:
                writer.close()
        assert not path.exists()

    asyncio.run(run())


def test_what_is_not_a_socket_at_the_path_is_refused_and_left(path):
    path.write_text("the operator's\n", encoding="utf-8")

    async def run() -> None:
        async with proxy(path, resolver=StubResolver(), dialer=StubDialer()):
            pass

    with pytest.raises(SpecfloError, match="not a socket"):
        asyncio.run(run())
    assert path.read_text(encoding="utf-8") == "the operator's\n"


def test_a_path_too_long_for_a_unix_socket_is_refused_by_name(short_dir):
    long = short_dir / ("s" * 120)

    async def run() -> None:
        async with proxy(long, resolver=StubResolver(), dialer=StubDialer()):
            pass

    with pytest.raises(SpecfloError, match="too long"):
        asyncio.run(run())


# The calls that would block the daemon's event loop, by their dotted name
# after imports are undone, and the modules whose import means one is made.
BLOCKING_CALLS = {
    "socket.socket", "socket.create_connection", "socket.create_server", "socket.socketpair",
    "socket.fromfd", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
    "socket.gethostbyaddr", "socket.getnameinfo", "socket.getfqdn",
    "httpx.Client", "httpx.get", "httpx.post", "httpx.put", "httpx.patch", "httpx.delete",
    "httpx.head", "httpx.options", "httpx.request", "httpx.stream",
    "time.sleep", "select.select",
}
BLOCKING_METHODS = {"recv", "recv_into", "recvfrom", "sendall", "accept", "connect", "makefile"}
BLOCKING_MODULES = {"requests", "urllib.request", "http.client", "urllib3"}


def blocking_uses(source: str) -> list[str]:
    """Every blocking call or import in *source*, as ``line: what``."""
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                aliases[alias.asname or top] = alias.name if alias.asname else top
                if any(alias.name == m or alias.name.startswith(m + ".") for m in BLOCKING_MODULES):
                    found.append(f"{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
            if any(module == m or module.startswith(m + ".") for m in BLOCKING_MODULES):
                found.append(f"{node.lineno}: from {module} import")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = dotted(node.func, aliases)
        if name in BLOCKING_CALLS:
            found.append(f"{node.lineno}: {name}()")
        elif isinstance(node.func, ast.Attribute) and node.func.attr in BLOCKING_METHODS:
            found.append(f"{node.lineno}: .{node.func.attr}()")
    return found


def dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(aliases.get(node.id, node.id))
    return ".".join(reversed(parts))


def test_the_proxy_module_makes_no_blocking_network_call():
    source = Path(webproxy.__file__).read_text(encoding="utf-8")
    assert blocking_uses(source) == []


@pytest.mark.parametrize("planted", [
    "import socket\nsocket.create_connection(('a', 1))\n",
    "import socket as s\ns.socket()\n",
    "from socket import getaddrinfo\ngetaddrinfo('a', 1)\n",
    "import requests\n",
    "import httpx\nhttpx.Client()\n",
    "from httpx import get\nget('http://a')\n",
    "import urllib.request\n",
    "def f(sock):\n    sock.sendall(b'x')\n",
])
def test_the_scan_finds_a_planted_blocking_call(planted):
    assert blocking_uses(planted)
