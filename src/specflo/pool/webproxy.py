"""The proxy a web member reaches the internet through, and what it refuses.

A member with web access has no route off this host of its own. Its sandbox
holds a forwarder to a unix socket the daemon serves, and the member's tools
find that forwarder as their HTTPS proxy. What arrives on the socket is one
of two requests: a CONNECT, which asks for a tunnel to a host and port and
carries HTTPS, and a plain HTTP request that names its whole URI. Both are
forwarded to a public host, and nothing else is.

The internet is not the only thing a proxy on this host can reach. The
rig's own services listen on loopback, the operator's network sits behind
private addresses, a cloud host answers for its credentials on a link-local
one, and a name anyone may register can resolve to any of these. So the
proxy resolves the host a request names, checks every address it gets
against the ranges that are not public, and refuses the request with 403,
before any connection is opened, when one of them is. A definition may list
host:port pairs the member is meant to reach inside (``web_internal``);
those are let through on their port and no other, whether a request names
the listed host or a name that resolves to a listed address.

The check holds only for the address that is dialled, so the proxy resolves
a name once and dials one of the addresses it checked, as a numeric host
that is not looked up again. A name that answers with a public address to
the check and a private one to the connect would otherwise walk straight
past it. Anything the proxy cannot read for certain, resolve or check is
refused: it fails closed.

The proxy runs on the daemon's event loop next to everything else the
daemon serves, so all of its I/O is asynchronous; the lookup goes through
the loop's resolver, which runs off the loop. The forwarder in the sandbox
is the member's to kill or replace, and a member that speaks to the socket
with a client of its own arrives here all the same: this module is the
boundary, not the forwarder.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import socket
import stat
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from ..errors import SpecfloError

log = logging.getLogger(__name__)

# What a request to a host that is not public is answered with. The host may
# well be there; the member may not have it.
REFUSED = 403

# The longest request line and headers the proxy reads before it answers. A
# member's tools send far less; a head that does not end within this is not
# read further.
_HEAD_MAX = 64 * 1024

# How long a client has to send its request line and headers, in seconds.
_HEAD_SECONDS = 30.0

# How long a lookup and a connect may take, in seconds, before the request is
# answered as unreachable.
_RESOLVE_SECONDS = 10.0
_CONNECT_SECONDS = 10.0

# How much is read from one side before it is written to the other.
_CHUNK = 64 * 1024

# The longest path a unix socket may be bound on: the kernel's field holds
# 108 bytes, the last of them the terminating zero.
_SOCKET_PATH_MAX = 107

# Headers that belong to one hop and are never passed on to the next. The
# proxy's own are among them: the credentials a client offers the proxy are
# not the upstream's to read.
_HOP_BY_HOP: frozenset[str] = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
})

# A method and a header name are tokens (RFC 9110).
_TOKEN = re.compile(r"\A[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")

# The prefix by which an IPv6 address carries an IPv4 one through a NAT64
# gateway. The standard library calls the whole prefix global; what decides
# is the IPv4 address inside it.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")

# An address as the resolver and the dialer pass it: the text of an IP address.
Resolver = Callable[[str, int], Awaitable[Iterable[str]]]
Dialer = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def resolve(host: str, port: int) -> list[str]:
    """The addresses *host* resolves to for a TCP connection to *port*.

    The loop's resolver runs the lookup off the loop.
    """
    found = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
    )
    return [info[4][0] for info in found]


async def dial(address: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A connection to the IP address *address* on *port*.

    The address is taken as a number and never looked up, so what is dialled
    is the address that was checked.
    """
    return await asyncio.open_connection(address, port, flags=socket.AI_NUMERICHOST, limit=_CHUNK)


def refused_address(address: str) -> bool:
    """Whether the proxy refuses to connect to *address*.

    It refuses every address that is not public: loopback, private (RFC 1918
    and unique local), link-local, shared (100.64.0.0/10), multicast,
    unspecified, reserved, and whatever else the standard library does not
    call global. An IPv6 address that carries an IPv4 one (mapped,
    compatible, 6to4, Teredo or NAT64) is refused when that one is. Text
    that is not an address is refused too.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    return any(_not_public(each) for each in _forms(ip))


def _forms(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """*ip*, and each IPv4 address it carries inside it."""
    yield ip
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            yield ip.ipv4_mapped
        if ip.sixtofour is not None:
            yield ip.sixtofour
        if ip.teredo is not None:
            yield from ip.teredo
        # IPv4-compatible (::a.b.c.d) and NAT64 (64:ff9b::a.b.c.d)
        if int(ip) >> 32 == 0 or ip in _NAT64:
            yield ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)


def _not_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # is_global alone lets multicast through, and an IPv4-compatible address
    return (
        not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_unspecified or ip.is_reserved
        or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)
    )


class _Answer(Exception):
    """The request is answered by the proxy itself, with *status*, and goes no further."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class _Request:
    """What the proxy read of one request: where it goes and what is sent there."""

    method: str
    host: str
    port: int
    # for a plain HTTP request: the head as it is sent upstream, and the
    # length of the body that follows it
    head: bytes = b""
    length: int = 0


class WebProxy:
    """The web proxy served on a unix socket at *path*, on the running loop.

    *internal* is the definition's ``web_internal``: the host:port pairs the
    proxy lets through although they are not public. *resolver* and *dialer*
    are how it looks a name up and connects to an address; ``resolve`` and
    ``dial`` unless given.

    ``start`` binds the socket, owner-only before it listens, and ``stop``
    ends every connection still open and removes it. The proxy is also an
    async context manager that does both.
    """

    def __init__(
        self,
        path: Path,
        internal: Iterable[str] = (),
        *,
        resolver: Resolver | None = None,
        dialer: Dialer | None = None,
    ) -> None:
        self.path = Path(path)
        self.internal = frozenset(_internal(entry) for entry in internal)
        self._resolve = resolver or resolve
        self._dial = dialer or dial
        self._server: asyncio.base_events.Server | None = None
        self._made: tuple[int, int] | None = None
        self._open: set[asyncio.Task] = set()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def start(self) -> None:
        """Serve the proxy on the socket at ``path``.

        Raises ``SpecfloError`` for a path too long to bind a socket on or one
        that holds something other than a socket, which is left as it is. A
        socket found at the path is replaced.
        """
        if self._server is not None:
            raise SpecfloError(f"The web proxy at {self.path} is served already.")
        if len(os.fsencode(self.path)) > _SOCKET_PATH_MAX:
            raise SpecfloError(
                f"The web proxy socket path {self.path} is too long for a unix socket, which "
                f"takes {_SOCKET_PATH_MAX} bytes at most; serve it from a shorter path."
            )
        try:
            found = os.lstat(self.path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(found.st_mode):
                raise SpecfloError(
                    f"{self.path} is there and is not a socket; the web proxy is served on "
                    "a socket at that path, so move what is there out of the way."
                )
        # bound, made owner-only, and only then listening: a socket that does
        # not listen yet refuses every connection
        server = await asyncio.start_unix_server(
            self._serve, path=str(self.path), limit=_HEAD_MAX, start_serving=False,
        )
        try:
            os.chmod(self.path, 0o600)
            self._made = _identity(self.path)
            await server.start_serving()
        except BaseException:
            server.close()
            self._unlink()
            raise
        self._server = server

    async def stop(self) -> None:
        """Stop serving, end every connection still open and remove the socket.

        A proxy that is not served is left as it is.
        """
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        still_open = list(self._open)
        for task in still_open:
            task.cancel()
        await asyncio.gather(*still_open, return_exceptions=True)
        await server.wait_closed()
        self._unlink()

    def _unlink(self) -> None:
        """Remove the socket, if what is at the path is still the one bound."""
        if self._made is not None and _identity(self.path) == self._made:
            self.path.unlink()
        self._made = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._open.add(task)
        upstream: asyncio.StreamWriter | None = None
        try:
            try:
                request = await asyncio.wait_for(_read(reader), _HEAD_SECONDS)
                addresses = await self._checked(request.host, request.port)
                from_upstream, upstream = await self._connect(request, addresses)
            except _Answer as answer:
                await _answer(writer, answer)
                return
            if request.method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await _tunnel(reader, writer, from_upstream, upstream)
            else:
                upstream.write(request.head)
                await _relay(reader, writer, from_upstream, upstream, request.length)
        except (_Closed, TimeoutError, ConnectionError):
            pass
        except Exception:
            # whatever went wrong, the request goes no further
            log.debug("web proxy request failed", exc_info=True)
        finally:
            for each in (upstream, writer):
                if each is not None:
                    each.close()
            if task is not None:
                self._open.discard(task)

    async def _checked(self, host: str, port: int) -> list[str]:
        """The addresses *host* resolves to, each of them checked.

        Raises ``_Answer`` with 403 when one of them is not public and
        host:port is not internal, and with 502 when the name gives no
        address.
        """
        literal = _literal(host)
        if literal is not None:
            addresses = [literal]
        else:
            try:
                found = await asyncio.wait_for(self._resolve(host, port), _RESOLVE_SECONDS)
                addresses = list(dict.fromkeys(_address(each) for each in found))
            except Exception:
                log.debug("web proxy lookup of %s failed", host, exc_info=True)
                raise _Answer(502, f"{host} could not be resolved.") from None
            if not addresses:
                raise _Answer(502, f"{host} resolves to no address.")
        if (host, port) in self.internal:
            return addresses
        for address in addresses:
            # an address listed with this port is as listed as a name is
            if refused_address(address) and (address, port) not in self.internal:
                raise _Answer(REFUSED, (
                    f"{_authority(host, port)} resolves to {address}, which is not a public "
                    "address; a member reaches an inside host only when its definition "
                    "lists that host and port under web_internal."
                ))
        return addresses

    async def _connect(self, request: _Request, addresses: list[str]):
        """A connection to the first of *addresses* that answers on the request's port."""
        for address in addresses:
            try:
                return await asyncio.wait_for(self._dial(address, request.port), _CONNECT_SECONDS)
            except (OSError, TimeoutError):
                continue
        raise _Answer(502, f"{_authority(request.host, request.port)} could not be reached.")


class _Closed(Exception):
    """The client went away before its request was whole."""


async def _read(reader: asyncio.StreamReader) -> _Request:
    """The request the client sends, read up to its body.

    Raises ``_Answer`` for one the proxy does not forward, and ``_Closed``
    when the client goes away first.
    """
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        raise _Answer(431, "The request's head is larger than the proxy reads.") from None
    except asyncio.IncompleteReadError:
        raise _Closed from None
    lines = raw[:-4].decode("latin-1").split("\r\n")
    if any("\r" in line or "\n" in line for line in lines):
        raise _Answer(400, "A line of the request ends in a bare CR or LF.")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not _TOKEN.match(parts[0]) or parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
        raise _Answer(400, "The request line is not 'METHOD target HTTP/1.x'.")
    method, target, _ = parts
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if not colon or not _TOKEN.match(name):
            raise _Answer(400, "A header of the request is not 'Name: value'.")
        headers.append((name, value.strip(" \t")))

    if method == "CONNECT":
        host, port = _host_port(target)
        return _Request(method, host, port)

    split = urlsplit(target)
    scheme = split.scheme.lower()
    if scheme == "https":
        raise _Answer(400, "An https URI is asked for with CONNECT, not sent to the proxy.")
    if scheme != "http" or not split.netloc:
        raise _Answer(400, "A request to the proxy names an absolute http URI, or is a CONNECT.")
    if "@" in split.netloc:
        raise _Answer(400, "The proxy does not forward a URI with credentials in it.")
    host, port = _host_port(split.netloc, default=80)
    if any(name.lower() == "transfer-encoding" for name, _ in headers):
        raise _Answer(501, "The proxy forwards a request body sent with Content-Length only.")
    lengths = {value for name, value in headers if name.lower() == "content-length"}
    if len(lengths) > 1 or not all(value.isdigit() and value.isascii() for value in lengths):
        raise _Answer(400, "The request's Content-Length is not one number.")
    length = int(lengths.pop()) if lengths else 0

    named = {
        token.strip().lower()
        for name, value in headers if name.lower() == "connection"
        for token in value.split(",")
    }
    passed = [
        f"{name}: {value}\r\n" for name, value in headers
        if name.lower() not in _HOP_BY_HOP | named | {"host"}
    ]
    origin = (split.path or "/") + (f"?{split.query}" if split.query else "")
    head = (
        f"{method} {origin} HTTP/1.1\r\nHost: {split.netloc}\r\n"
        + "".join(passed)
        + "Connection: close\r\n\r\n"
    )
    return _Request(method, host, port, head.encode("latin-1"), length)


def _host_port(authority: str, default: int | None = None) -> tuple[str, int]:
    """The host, as ``_host`` gives it, and port of *authority*, ``host:port`` or ``[v6]:port``.

    Raises ``_Answer`` with 400 for one that is not that.
    """
    bad = _Answer(400, f"{authority!r} is not a host and a port.")
    if authority.startswith("["):
        host, bracket, rest = authority[1:].partition("]")
        if not bracket or (rest and not rest.startswith(":")):
            raise bad
        port_text = rest[1:] if rest else ""
    else:
        host, colon, port_text = authority.rpartition(":")
        if not colon:
            host, port_text = authority, ""
        if ":" in host:
            raise bad
    if not port_text and default is not None:
        port = default
    elif port_text.isdigit() and port_text.isascii() and 1 <= int(port_text) <= 65535:
        port = int(port_text)
    else:
        raise bad
    host = _host(host)
    if not host:
        raise bad
    return host, port


def _host(host: str) -> str:
    """*host* as the proxy compares it: an address in its usual text, a name
    in lower case without a trailing dot."""
    literal = _literal(host)
    return literal if literal is not None else host.lower().rstrip(".")


def _literal(host: str) -> str | None:
    """*host* as the text of an IP address, None when it is not one."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def _address(found: object) -> str:
    """An address the resolver gave, as the text of an IP address."""
    return str(ipaddress.ip_address(str(found)))


def _authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _internal(entry: str) -> tuple[str, int]:
    """A ``web_internal`` entry as the host and port the proxy compares."""
    try:
        return _host_port(entry.strip())
    except _Answer:
        raise SpecfloError(
            f"The web_internal entry {entry!r} is not host:port; give each host with its "
            "port, such as '127.0.0.1:8731'."
        ) from None


async def _answer(writer: asyncio.StreamWriter, answer: _Answer) -> None:
    """Answer the client with *answer*, and nothing else."""
    body = f"{answer.message}\n".encode()
    head = (
        f"HTTP/1.1 {answer.status} {HTTPStatus(answer.status).phrase}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    writer.write(head.encode("ascii") + body)
    with contextlib.suppress(ConnectionError):
        await writer.drain()


async def _pipe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, length: int | None = None,
) -> None:
    """Copy from *reader* to *writer*: *length* bytes, or until the end, after
    which *writer* is told no more is coming."""
    left = length
    while left is None or left > 0:
        chunk = await reader.read(_CHUNK if left is None else min(_CHUNK, left))
        if not chunk:
            if left is not None:
                raise _Closed
            break
        writer.write(chunk)
        await writer.drain()
        if left is not None:
            left -= len(chunk)
    if left is None and writer.can_write_eof():
        writer.write_eof()


async def _tunnel(
    client: asyncio.StreamReader, to_client: asyncio.StreamWriter,
    upstream: asyncio.StreamReader, to_upstream: asyncio.StreamWriter,
) -> None:
    """Copy both ways until each side has said all it has to say."""
    async with asyncio.TaskGroup() as both:
        both.create_task(_pipe(client, to_upstream))
        both.create_task(_pipe(upstream, to_client))


async def _relay(
    client: asyncio.StreamReader, to_client: asyncio.StreamWriter,
    upstream: asyncio.StreamReader, to_upstream: asyncio.StreamWriter,
    length: int,
) -> None:
    """Send the request's body up and the answer down, until the answer ends.

    The two run side by side, so an upstream that answers before the body is
    whole, or asks the client to go on, is heard. Nothing the client sends
    after the body is read: the next request is a new connection.
    """
    body = asyncio.create_task(_pipe(client, to_upstream, length))
    try:
        await _pipe(upstream, to_client)
    finally:
        body.cancel()
        with contextlib.suppress(asyncio.CancelledError, _Closed, ConnectionError):
            await body


def _identity(path: Path) -> tuple[int, int] | None:
    """The device and inode at *path*, None when nothing is there."""
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return None
    return found.st_dev, found.st_ino
