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
import datetime
import ipaddress
import json
import os
import shutil
import stat
import struct
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


# -- SOCKS5 on the same socket ----------------------------------------------
#
# Some tools take only a SOCKS5 proxy, and resolve every name themselves
# unless it is one: donsetch is such a tool, and a member's sandbox has no
# DNS. The proxy tells a SOCKS5 client from an HTTP one by its first byte and
# holds it to the same check: the name is resolved once, by the proxy, and the
# address it checked is the one dialled.

PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"

# A SOCKS5 greeting that offers no authentication, and the method the proxy
# picks in answer to it.
GREETING = b"\x05\x01\x00"
NO_AUTH = b"\x05\x00"

# The reply codes of RFC 1928 the tests look for.
SUCCEEDED, GENERAL_FAILURE, NOT_ALLOWED = 0x00, 0x01, 0x02
HOST_UNREACHABLE, CONNECTION_REFUSED = 0x04, 0x05
COMMAND_NOT_SUPPORTED, ADDRESS_NOT_SUPPORTED = 0x07, 0x08


def socks_address(host: str) -> bytes:
    """*host* as a SOCKS5 address: its type byte and the address."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        name = host.encode("ascii")
        return bytes((0x03, len(name))) + name
    return (b"\x01" if ip.version == 4 else b"\x04") + ip.packed


def socks_connect(host: str, port: int, command: int = 0x01, version: int = 0x05) -> bytes:
    """The request a SOCKS5 client sends for *command* to *host* on *port*."""
    return bytes((version, command, 0x00)) + socks_address(host) + port.to_bytes(2, "big")


def socks_reply(code: int) -> bytes:
    """The whole of a SOCKS5 reply with *code*, bound to no address."""
    return bytes((0x05, code, 0x00, 0x01)) + bytes(6)


async def socks_open(path: Path, host: str, port: int):
    """A SOCKS5 CONNECT to *host* on *port* through the proxy at *path*,
    with the method and the reply it was answered with read."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(GREETING)
    await writer.drain()
    method = await asyncio.wait_for(reader.readexactly(2), 10)
    writer.write(socks_connect(host, port))
    await writer.drain()
    reply = await asyncio.wait_for(reader.readexactly(10), 10)
    return method, reply, reader, writer


def socks_through(path: Path, resolver: StubResolver, host: str, port: int = 443, internal=()):
    """Open a SOCKS5 tunnel to *host*, send a GET through it, and return the
    reply, the answer, the upstream's heads and the addresses dialled."""

    async def run():
        async with Upstream() as upstream:
            dialer = StubDialer(upstream.address)
            async with proxy(path, internal, resolver=resolver, dialer=dialer):
                method, reply, reader, writer = await socks_open(path, host, port)
                try:
                    writer.write(b"GET /through HTTP/1.1\r\nHost: tunnel\r\n\r\n")
                    await writer.drain()
                    answer = await asyncio.wait_for(reader.read(), 10)
                finally:
                    writer.close()
            assert method == NO_AUTH
            return reply, answer, upstream.heads, dialer.calls

    return asyncio.run(run())


def test_a_socks_connect_to_a_public_name_is_tunnelled_to_the_address_it_resolved_to(path):
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC]})

    reply, answer, heads, dialled = socks_through(path, resolver, PUBLIC_NAME)

    assert reply == socks_reply(SUCCEEDED)
    assert status(answer) == 200
    assert heads[0].startswith("GET /through HTTP/1.1\r\n")
    assert resolver.calls == [(PUBLIC_NAME, 443)]
    assert dialled == [(PUBLIC, 443)]


@pytest.mark.parametrize("address", [PUBLIC, PUBLIC_V6], ids=["ipv4", "ipv6"])
def test_a_socks_connect_to_a_public_address_is_dialled_with_no_lookup(path, address):
    resolver = StubResolver()

    reply, answer, _, dialled = socks_through(path, resolver, address, 8443)

    assert reply == socks_reply(SUCCEEDED)
    assert status(answer) == 200
    assert resolver.calls == []
    assert dialled == [(address, 8443)]


def socks_answer(path: Path, request: bytes, **stubs) -> bytes:
    """Send *request* to a proxy at *path* served with *stubs*, and read all it answers."""

    async def run() -> bytes:
        async with proxy(path, **stubs):
            return await exchange(path, request)

    return asyncio.run(run())


@pytest.mark.parametrize("address", REFUSED_ADDRESSES.values(), ids=REFUSED_ADDRESSES.keys())
@pytest.mark.parametrize("form", ["socks literal", "socks name"])
def test_a_socks_connect_to_a_host_that_is_not_public_is_refused_with_no_connection(
    path, address, form,
):
    resolver = StubResolver({"named.example": [address]})
    dialer = StubDialer()
    host = address if form == "socks literal" else "named.example"

    answer = socks_answer(
        path, GREETING + socks_connect(host, 443), resolver=resolver, dialer=dialer,
    )

    assert answer == NO_AUTH + socks_reply(NOT_ALLOWED)
    assert dialer.calls == []
    if form == "socks literal":
        assert resolver.calls == []


def test_a_socks_name_that_resolves_to_a_private_address_is_refused(path):
    resolver = StubResolver({"news.example": ["10.0.0.5"]})
    dialer = StubDialer()

    answer = socks_answer(
        path, GREETING + socks_connect("news.example", 443), resolver=resolver, dialer=dialer,
    )

    assert answer == NO_AUTH + socks_reply(NOT_ALLOWED)
    assert resolver.calls == [("news.example", 443)]
    assert dialer.calls == []


def test_a_socks_connect_to_a_listed_internal_host_is_allowed_on_its_port_only(path):
    resolver = StubResolver({"intranet.lan": ["127.0.0.1"]})

    async def run() -> tuple[bytes, bytes, list]:
        async with Upstream() as upstream:
            dialer = StubDialer(upstream.address)
            async with proxy(path, ["intranet.lan:8731"], resolver=resolver, dialer=dialer):
                _, listed, _, writer = await socks_open(path, "intranet.lan", 8731)
                writer.close()
                other = await exchange(path, GREETING + socks_connect("intranet.lan", 8732))
            return listed, other, dialer.calls

    listed, other, dialled = asyncio.run(run())
    assert listed == socks_reply(SUCCEEDED)
    assert other == NO_AUTH + socks_reply(NOT_ALLOWED)
    assert dialled == [("127.0.0.1", 8731)]


@pytest.mark.parametrize("resolver, code", [
    (StubResolver(error=OSError("no such name")), HOST_UNREACHABLE),
    (StubResolver({}), HOST_UNREACHABLE),
    (StubResolver({PUBLIC_NAME: [PUBLIC]}), CONNECTION_REFUSED),
], ids=["lookup fails", "no address", "upstream refuses"])
def test_a_socks_connect_that_cannot_reach_its_host_is_answered_unreachable(path, resolver, code):
    answer = socks_answer(
        path, GREETING + socks_connect(PUBLIC_NAME, 443), resolver=resolver, dialer=StubDialer(),
    )

    assert answer == NO_AUTH + socks_reply(code)


@pytest.mark.parametrize("request_bytes, expected", [
    # only username and password offered: no method is acceptable
    (b"\x05\x01\x02" + socks_connect(PUBLIC_NAME, 443), b"\x05\xff"),
    # no method offered at all
    (b"\x05\x00", b"\x05\xff"),
    # BIND and UDP ASSOCIATE
    (GREETING + socks_connect(PUBLIC_NAME, 443, command=0x02),
     NO_AUTH + socks_reply(COMMAND_NOT_SUPPORTED)),
    (GREETING + socks_connect(PUBLIC_NAME, 443, command=0x03),
     NO_AUTH + socks_reply(COMMAND_NOT_SUPPORTED)),
    # a request of another version than the greeting's
    (GREETING + socks_connect(PUBLIC_NAME, 443, version=0x04), NO_AUTH + socks_reply(GENERAL_FAILURE)),
    # an address type RFC 1928 does not define
    (GREETING + b"\x05\x01\x00\x09" + bytes(6), NO_AUTH + socks_reply(ADDRESS_NOT_SUPPORTED)),
    # port zero, an empty name, and a name with a port in it
    (GREETING + socks_connect(PUBLIC_NAME, 0), NO_AUTH + socks_reply(GENERAL_FAILURE)),
    (GREETING + b"\x05\x01\x00\x03\x00\x01\xbb", NO_AUTH + socks_reply(GENERAL_FAILURE)),
    (GREETING + socks_connect("public.example:80", 443), NO_AUTH + socks_reply(GENERAL_FAILURE)),
    (GREETING + b"\x05\x01\x00\x03\x04a b\x00\x01\xbb", NO_AUTH + socks_reply(GENERAL_FAILURE)),
], ids=[
    "password only", "no method", "bind", "udp associate", "request version 4",
    "unknown address type", "port zero", "empty name", "name with a port", "name with a space",
])
def test_a_socks_request_the_proxy_does_not_serve_fails_closed(path, request_bytes, expected):
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC]})
    dialer = StubDialer()

    answer = socks_answer(path, request_bytes, resolver=resolver, dialer=dialer)

    assert answer == expected
    assert resolver.calls == []
    assert dialer.calls == []


@pytest.mark.parametrize("request_bytes", [
    # SOCKS4: CONNECT to 93.184.215.14:443 for user "u"
    b"\x04\x01\x01\xbb" + bytes((93, 184, 215, 14)) + b"u\x00",
    # SOCKS4a: the name follows the user
    b"\x04\x01\x01\xbb\x00\x00\x00\x01u\x00public.example\x00",
    b"\x00\x01\x02\x03",
], ids=["socks4", "socks4a", "binary"])
def test_a_client_that_speaks_neither_socks5_nor_http_is_refused_at_once(path, request_bytes):
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC]})
    dialer = StubDialer()

    answer = socks_answer(path, request_bytes, resolver=resolver, dialer=dialer)

    assert status(answer) == 400
    assert resolver.calls == []
    assert dialer.calls == []


# -- The host's own addresses -----------------------------------------------
#
# A host with a public IPv4 address or an IPv6 global address serves its own
# services there as well as on loopback: whatever listens on 0.0.0.0 or ::,
# llama-swap and the daemon among them. Such an address passes the range
# check, so the proxy asks at each request whether an address is one of the
# host's own and refuses it when it is. The check here is a stub that says
# which addresses the test makes the host's own.

HOST_V4 = "93.184.216.34"
HOST_V6 = "2a01:4f8:c0c:1::5"
HOST_NAME = "self.example"


class StubLocal:
    """The host's own addresses as a test gives them; every check is recorded."""

    def __init__(self, addresses: Iterable[str] = (), *, error: Exception | None = None):
        self.addresses = set(addresses)
        self.error = error
        self.calls: list[str] = []

    async def __call__(self, address: str) -> bool:
        self.calls.append(address)
        if self.error is not None:
            raise self.error
        return address in self.addresses


def logged_verdicts(root: Path) -> list[tuple[str, int, str]]:
    today = datetime.datetime.now(datetime.timezone.utc).date()
    lines = webproxy.log_path(root, today).read_text(encoding="utf-8").splitlines()
    return [(line["host"], line["port"], line["verdict"]) for line in map(json.loads, lines)]


def own_request(form: str, address: str, port: int = 443) -> tuple[bytes, str]:
    """The request of *form* to *address*, or to a name that resolves to it,
    and the host the proxy reads in it."""
    literal = f"[{address}]" if ":" in address else address
    host = webproxy._host(address)
    return {
        "connect literal": (f"CONNECT {literal}:{port} HTTP/1.1\r\n\r\n".encode(), host),
        "connect name": (f"CONNECT {HOST_NAME}:{port} HTTP/1.1\r\n\r\n".encode(), HOST_NAME),
        "absolute uri literal": (
            f"GET http://{literal}:{port}/ HTTP/1.1\r\nHost: {literal}\r\n\r\n".encode(), host,
        ),
        "absolute uri name": (
            f"GET http://{HOST_NAME}:{port}/ HTTP/1.1\r\nHost: {HOST_NAME}\r\n\r\n".encode(),
            HOST_NAME,
        ),
        "socks literal": (GREETING + socks_connect(address, port), host),
        "socks name": (GREETING + socks_connect(HOST_NAME, port), HOST_NAME),
    }[form]


OWN_FORMS = [
    "connect literal", "connect name", "absolute uri literal", "absolute uri name",
    "socks literal", "socks name",
]


@pytest.mark.parametrize("address", [
    HOST_V4, HOST_V6, f"::ffff:{HOST_V4}",
], ids=["public ipv4", "ipv6 global", "ipv4-mapped public ipv4"])
@pytest.mark.parametrize("form", OWN_FORMS)
def test_one_of_the_hosts_own_addresses_is_refused_and_logged_with_no_connection(
    path, address, form,
):
    # the stub holds the IPv4 address alone, so the mapped form is refused for
    # the address it carries
    local = StubLocal({HOST_V4, HOST_V6})
    resolver = StubResolver({HOST_NAME: [address]})
    dialer = StubDialer()
    root = path.parent / "daemon"
    request, host = own_request(form, address)

    async def run() -> bytes:
        async with proxy(path, resolver=resolver, dialer=dialer, local_check=local, root=root):
            return await exchange(path, request)

    answer = asyncio.run(run())
    # Python 3.12 calls a mapped address reserved, so the range check refuses
    # it before the host is asked; later versions leave it to the host check
    by_range = webproxy.refused_address(address)
    if form.startswith("socks"):
        assert answer == NO_AUTH + socks_reply(NOT_ALLOWED)
    else:
        assert status(answer) == webproxy.REFUSED
        assert by_range or b"own" in answer
    assert dialer.calls == []
    assert by_range or local.calls
    assert logged_verdicts(root) == [(host, 443, "refused")]


@pytest.mark.parametrize("form", OWN_FORMS)
@pytest.mark.parametrize("address", [HOST_V4, HOST_V6], ids=["public ipv4", "ipv6 global"])
def test_a_listed_internal_host_and_port_on_one_of_the_hosts_own_addresses_is_allowed(
    path, address, form,
):
    local = StubLocal({HOST_V4, HOST_V6})
    resolver = StubResolver({HOST_NAME: [address]})
    literal = f"[{address}]" if ":" in address else address
    root = path.parent / "daemon"
    request, host = own_request(form, address, 8731)
    other, _ = own_request(form, address, 8732)

    async def run() -> tuple[bytes, bytes, list]:
        async with Upstream() as upstream:
            dialer = StubDialer(upstream.address)
            async with proxy(
                path, [f"{literal}:8731"], resolver=resolver, dialer=dialer,
                local_check=local, root=root,
            ):
                if form.startswith("socks"):
                    _, listed, _, writer = await socks_open(
                        path, HOST_NAME if form.endswith("name") else address, 8731,
                    )
                    writer.close()
                else:
                    listed = await answer_head(path, request)
                refused = await exchange(path, other)
            return listed, refused, dialer.calls

    listed, refused, dialled = asyncio.run(run())
    if form.startswith("socks"):
        assert listed == socks_reply(SUCCEEDED)
        assert refused == NO_AUTH + socks_reply(NOT_ALLOWED)
    else:
        assert status(listed) == 200
        assert status(refused) == webproxy.REFUSED
    assert dialled == [(address, 8731)]
    assert (host, 8732, "refused") in logged_verdicts(root)


def test_the_hosts_own_addresses_are_checked_at_each_request(path):
    # an address the host takes on after the proxy started is refused from then on
    local = StubLocal()
    resolver = StubResolver({HOST_NAME: [HOST_V4]})

    async def run() -> tuple[bytes, bytes]:
        async with Upstream() as upstream:
            async with proxy(
                path, resolver=resolver, dialer=StubDialer(upstream.address), local_check=local,
            ):
                before = await answer_head(path, b"CONNECT self.example:443 HTTP/1.1\r\n\r\n")
                local.addresses.add(HOST_V4)
                after = await exchange(path, b"CONNECT self.example:443 HTTP/1.1\r\n\r\n")
            return before, after

    before, after = asyncio.run(run())
    assert status(before) == 200
    assert status(after) == webproxy.REFUSED
    assert local.calls == [HOST_V4, HOST_V4]


@pytest.mark.parametrize("form", ["connect literal", "socks name"])
def test_a_check_of_the_hosts_own_addresses_that_fails_refuses(path, form):
    local = StubLocal(error=OSError("no answer"))
    resolver = StubResolver({HOST_NAME: [HOST_V4]})
    dialer = StubDialer()
    root = path.parent / "daemon"
    request, host = own_request(form, HOST_V4)

    async def run() -> bytes:
        async with proxy(path, resolver=resolver, dialer=dialer, local_check=local, root=root):
            return await exchange(path, request)

    answer = asyncio.run(run())
    if form.startswith("socks"):
        assert answer == NO_AUTH + socks_reply(NOT_ALLOWED)
    else:
        assert status(answer) == webproxy.REFUSED
    assert dialer.calls == []
    assert logged_verdicts(root) == [(host, 443, "refused")]


def test_an_address_that_is_not_public_is_refused_before_the_host_is_asked(path):
    local = StubLocal()

    async def run() -> bytes:
        async with proxy(path, resolver=StubResolver(), dialer=StubDialer(), local_check=local):
            return await exchange(path, b"CONNECT 10.0.0.5:443 HTTP/1.1\r\n\r\n")

    assert status(asyncio.run(run())) == webproxy.REFUSED
    assert local.calls == []


def test_the_default_check_knows_loopback_as_the_hosts_own_and_a_far_address_as_not():
    async def run() -> tuple[bool, bool]:
        return await webproxy.is_local_address("127.0.0.1"), await webproxy.is_local_address(PUBLIC)

    assert asyncio.run(run()) == (True, False)


def test_the_default_check_raises_for_text_that_is_not_an_address():
    with pytest.raises(ValueError):
        asyncio.run(webproxy.is_local_address("not an address"))


# -- DNS for the member's own lookups ---------------------------------------
#
# A tool that resolves every name itself, even through a SOCKS5 proxy, needs
# a resolver inside the sandbox. The daemon serves one on a second unix
# socket per lease: each connection carries one DNS query, which goes to the
# host's resolver unchanged, and the answer comes back unchanged. The query is
# read only far enough to log its name and type; one the forwarder cannot
# read is answered SERVFAIL, or not at all when it has no header to answer.
# The upstream here is a stub, so nothing leaves this machine.

A, AAAA = 1, 28
SERVFAIL = 2


def dns_query(name: str, qtype: int = A, ident: int = 0x1234, flags: int = 0x0100,
              counts: tuple[int, int, int, int] = (1, 0, 0, 0)) -> bytes:
    """A DNS query for *name* and *qtype*, recursion desired, as a stub resolver sends it."""
    labels = b"".join(bytes((len(part),)) + part.encode() for part in name.split(".") if part)
    return (
        struct.pack("!HH4H", ident, flags, *counts)
        + labels + b"\x00" + struct.pack("!HH", qtype, 1)
    )


def dns_answer(query: bytes, address: str) -> bytes:
    """The answer to the A *query*: *address*, for a minute."""
    ident = query[:2]
    return (
        ident + struct.pack("!H4H", 0x8180, 1, 1, 0, 0) + query[12:]
        + b"\xc0\x0c" + struct.pack("!HHIH", A, 1, 60, 4)
        + ipaddress.IPv4Address(address).packed
    )


class StubUpstream:
    """The host's resolver as a test gives it: names mapped to one address each.

    Every query it is sent is recorded; *error* is raised instead of an answer.
    """

    def __init__(self, names: dict[str, str] | None = None, *, error: Exception | None = None,
                 answer: bytes | None = None):
        self.names = names or {}
        self.error = error
        self.answer = answer
        self.queries: list[bytes] = []

    async def __call__(self, query: bytes) -> bytes:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        if self.answer is not None:
            return self.answer
        name, qtype, end = question(query)
        if qtype != A:
            # no record of any other type: an answer with nothing in it
            return query[:2] + struct.pack("!H4H", 0x8180, 1, 0, 0, 0) + query[12:end]
        return dns_answer(query[:end], self.names.get(name, "0.0.0.0"))


def question(query: bytes) -> tuple[str, int, int]:
    """The name, in lower case, and the type a query asks for, and where
    its question ends."""
    labels, at = [], 12
    while query[at]:
        labels.append(query[at + 1 : at + 1 + query[at]].decode().lower())
        at += 1 + query[at]
    return ".".join(labels), struct.unpack("!H", query[at + 1 : at + 3])[0], at + 5


def dns(path: Path, **kwargs) -> webproxy.DnsForwarder:
    return webproxy.DnsForwarder(path, **kwargs)


async def ask(path: Path, query: bytes) -> bytes:
    """Send *query* to the DNS socket at *path* as the sandbox's forwarder
    does, the end of it said, and read the answer to the end."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(query)
        await writer.drain()
        writer.write_eof()
        return await asyncio.wait_for(reader.read(), 10)
    finally:
        writer.close()


def rcode(answer: bytes) -> int:
    return answer[3] & 0x0F


@pytest.fixture
def dns_path(short_dir: Path) -> Path:
    return short_dir / "web.dns"


def test_the_dns_socket_is_served_beside_the_proxy_socket():
    assert webproxy.dns_path(Path("/r/web/lease-1.sock")) == Path("/r/web/lease-1.dns")


def test_a_query_goes_to_the_upstream_unchanged_and_its_answer_comes_back_unchanged(dns_path):
    upstream = StubUpstream({"research.example": "203.0.113.7"})
    query = dns_query("research.example")

    async def run() -> bytes:
        async with dns(dns_path, upstream=upstream):
            return await ask(dns_path, query)

    answer = asyncio.run(run())
    assert upstream.queries == [query]
    assert answer == dns_answer(query, "203.0.113.7")


def test_each_query_leaves_one_line_naming_its_name_and_type(dns_path, short_dir):
    root = short_dir / "daemon"
    upstream = StubUpstream({"research.example": "203.0.113.7"})

    async def run() -> None:
        async with dns(dns_path, upstream=upstream, lease="lease-1", member="m", root=root):
            await ask(dns_path, dns_query("Research.Example", A))
            await ask(dns_path, dns_query("research.example", AAAA))

    asyncio.run(run())
    today = datetime.datetime.now(datetime.timezone.utc).date()
    found = [json.loads(line) for line in webproxy.log_path(root, today).read_text().splitlines()]
    assert [(line["host"], line["type"], line["port"], line["verdict"]) for line in found] == [
        ("research.example", "A", None, "dns"), ("research.example", "AAAA", None, "dns"),
    ]
    assert {line["lease"] for line in found} == {"lease-1"}
    assert {line["member"] for line in found} == {"m"}
    assert found[0]["bytes_up"] == len(dns_query("Research.Example"))
    assert found[0]["bytes_down"] > 0


@pytest.mark.parametrize("query", [
    dns_query("research.example", flags=0x8180),               # an answer, not a query
    dns_query("research.example", flags=0x2900),               # an update, not a query
    dns_query("research.example", counts=(2, 0, 0, 0)),        # two questions
    dns_query("research.example", counts=(1, 1, 0, 0)),        # a query with an answer in it
    dns_query("research.example")[:-3],                         # the question cut short
    dns_query("research.example")[:12] + b"\xc0\x0c\x00\x01\x00\x01",  # a pointer for a name
    dns_query("research.example")[:12] + b"\x05a\x00b\x00\x00\x00\x01\x00\x01",  # NUL in a label
], ids=["answer", "update", "two questions", "with an answer", "cut short", "pointer", "nul"])
def test_a_query_the_forwarder_cannot_read_is_answered_servfail_and_not_forwarded(dns_path, query):
    upstream = StubUpstream()

    async def run() -> bytes:
        async with dns(dns_path, upstream=upstream):
            return await ask(dns_path, query)

    answer = asyncio.run(run())
    assert upstream.queries == []
    assert answer[:2] == query[:2]
    assert answer[2] & 0x80  # a response
    assert rcode(answer) == SERVFAIL


@pytest.mark.parametrize("query", [b"", b"\x12\x34\x01", b"\x00" * 70000],
                         ids=["nothing", "no header", "larger than a datagram"])
def test_what_has_no_header_or_is_too_large_is_dropped_with_no_answer(dns_path, query):
    upstream = StubUpstream()

    async def run() -> bytes:
        async with dns(dns_path, upstream=upstream):
            reader, writer = await asyncio.open_unix_connection(str(dns_path))
            try:
                writer.write(query)
                await writer.drain()
                writer.write_eof()
                return await asyncio.wait_for(reader.read(), 10)
            except ConnectionError:
                return b""
            finally:
                writer.close()

    assert asyncio.run(run()) == b""
    assert upstream.queries == []


@pytest.mark.parametrize("upstream", [
    StubUpstream(error=TimeoutError()),
    StubUpstream(error=OSError("unreachable")),
    StubUpstream(answer=b"\x00\x01"),
    StubUpstream(answer=dns_answer(dns_query("x.example", ident=0x9999), "203.0.113.7")),
], ids=["timeout", "unreachable", "short answer", "another query's answer"])
def test_an_upstream_that_does_not_answer_this_query_is_answered_servfail(dns_path, upstream):
    query = dns_query("research.example")

    async def run() -> bytes:
        async with dns(dns_path, upstream=upstream):
            return await ask(dns_path, query)

    answer = asyncio.run(run())
    assert answer[:2] == query[:2]
    assert rcode(answer) == SERVFAIL
    # the question is echoed, so the stub resolver takes the answer as its own
    assert answer[12:] == query[12:]
    assert struct.unpack("!H", answer[4:6]) == (1,)


def test_the_dns_socket_is_owner_only_while_served_and_gone_after(dns_path):
    async def run() -> None:
        served = dns(dns_path, upstream=StubUpstream())
        assert served.path == dns_path
        await served.start()
        try:
            mode = os.stat(dns_path).st_mode
            assert stat.S_ISSOCK(mode)
            assert stat.S_IMODE(mode) == 0o600
        finally:
            await served.stop()
        assert not dns_path.exists()
        await served.stop()  # a second stop is a no-op

    asyncio.run(run())


def test_what_is_not_a_socket_at_the_dns_path_is_refused_and_left(dns_path):
    dns_path.write_text("the operator's\n", encoding="utf-8")

    async def run() -> None:
        async with dns(dns_path, upstream=StubUpstream()):
            pass

    with pytest.raises(SpecfloError, match="not a socket"):
        asyncio.run(run())
    assert dns_path.read_text(encoding="utf-8") == "the operator's\n"


@pytest.mark.parametrize("text, expected", [
    ("# generated\nnameserver 127.0.0.53\noptions edns0 trust-ad\nsearch .\n", "127.0.0.53"),
    ("search lan\nnameserver ::1\nnameserver 10.0.0.1\n", "::1"),
    ("nameserver not-an-address\nnameserver 192.0.2.53\n", "192.0.2.53"),
    ("options rotate\n", "127.0.0.1"),
    ("", "127.0.0.1"),
], ids=["resolved stub", "first of two", "skips what is no address", "none", "empty"])
def test_the_host_resolver_is_the_first_nameserver_the_host_names(text, expected):
    # with none named, the C library asks the local host, and so does the forwarder
    assert webproxy.host_nameserver(text) == expected


class UdpResolver(asyncio.DatagramProtocol):
    """A resolver on a loopback UDP port: it answers each query first with
    the answer to another query, then with its own."""

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.queries: list[bytes] = []

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.queries.append(data)
        other = bytes((data[0] ^ 0xFF,)) + data[1:]
        self.transport.sendto(dns_answer(other, "198.51.100.1"), addr)
        self.transport.sendto(dns_answer(data, "203.0.113.7"), addr)


def test_the_udp_upstream_takes_the_answer_to_its_own_query_alone():
    query = dns_query("research.example")

    async def run() -> tuple[bytes, list[bytes]]:
        loop = asyncio.get_running_loop()
        transport, resolver = await loop.create_datagram_endpoint(
            UdpResolver, local_addr=("127.0.0.1", 0),
        )
        try:
            port = transport.get_extra_info("sockname")[1]
            answer = await webproxy.UdpUpstream("127.0.0.1", port, timeout=5)(query)
            return answer, resolver.queries
        finally:
            transport.close()

    answer, queries = asyncio.run(run())
    assert queries == [query]
    assert answer == dns_answer(query, "203.0.113.7")


def test_the_udp_upstream_gives_up_after_its_timeout():
    async def run() -> None:
        loop = asyncio.get_running_loop()
        # a port that takes the query and never answers
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0),
        )
        try:
            port = transport.get_extra_info("sockname")[1]
            with pytest.raises(TimeoutError):
                await webproxy.UdpUpstream("127.0.0.1", port, timeout=0.2)(dns_query("a.example"))
        finally:
            transport.close()

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
