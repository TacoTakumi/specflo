"""The proxy a web member reaches the internet through, and what it refuses.

A member with web access has no route off this host of its own. Its sandbox
holds a forwarder to a unix socket the daemon serves, and the member's tools
find that forwarder as their HTTPS proxy. What arrives on the socket is one
of three requests: a CONNECT, which asks for a tunnel to a host and port and
carries HTTPS, a plain HTTP request that names its whole URI, and a SOCKS5
CONNECT (RFC 1928, with no authentication), which asks for a tunnel too and
is told from the other two by its first byte. A tool that resolves every
name it is given itself takes a SOCKS5 proxy with remote lookups instead.
All three are forwarded to a public host, and nothing else is.

Some tools resolve a name themselves even then, before they hand it to the
proxy, so the sandbox has DNS too: a second socket of the lease's own,
``dns_path`` beside the proxy's, takes each query a forwarder inside brings
it and answers it through the host's resolver (``DnsForwarder``). An answer
is an address and no route to it: the connection still goes through the
proxy, which resolves the name again and checks what it dials.

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
import datetime
import ipaddress
import json
import logging
import os
import re
import socket
import stat
import struct
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from ..errors import SpecfloError

log = logging.getLogger(__name__)

# The directory under the daemon root that holds the request log, one
# JSON-lines file per day (UTC) named for it.
LOG_DIRNAME = "webproxy"

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

# What RFC 1928 has a SOCKS5 client and the proxy say to each other: the
# version byte that starts each message, the one method the proxy accepts (no
# authentication) and the answer when that is not offered, the one command it
# serves (CONNECT), and the three address types.
_SOCKS5 = 0x05
_NO_AUTH = 0x00
_NO_ACCEPTABLE = 0xFF
_SOCKS_CONNECT = 0x01
_IPV4, _DOMAIN, _IPV6 = 0x01, 0x03, 0x04

# The reply codes the proxy answers a SOCKS5 request with.
_SUCCEEDED = 0x00
_GENERAL_FAILURE = 0x01
_NOT_ALLOWED = 0x02
_HOST_UNREACHABLE = 0x04
_CONNECTION_REFUSED = 0x05
_COMMAND_NOT_SUPPORTED = 0x07
_ADDRESS_NOT_SUPPORTED = 0x08

# The prefix by which an IPv6 address carries an IPv4 one through a NAT64
# gateway. The standard library calls the whole prefix global; what decides
# is the IPv4 address inside it.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")

# An address as the resolver and the dialer pass it: the text of an IP address.
Resolver = Callable[[str, int], Awaitable[Iterable[str]]]
Dialer = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

# What the DNS forwarder asks a query of: the query as it came, the answer as
# it goes back.
DnsUpstream = Callable[[bytes], Awaitable[bytes]]

# What a lease's DNS socket ends in, beside its proxy socket.
DNS_SUFFIX = ".dns"

# Where the host names its resolver.
RESOLVER = "/etc/resolv.conf"

# How long the host's resolver may take to answer, in seconds. A stub
# resolver waits five for an answer before it asks again, and SERVFAIL
# within that is an answer it takes.
_DNS_SECONDS = 4.0

# A DNS message: its header, the longest one a datagram holds, the response
# bit in its third byte, and the answer code for a server that failed.
_DNS_HEADER = 12
_DNS_MAX = 65535
_QR = 0x80
_SERVFAIL = 2

# The record types a query is logged with by name; any other is logged as
# TYPE and its number (RFC 3597).
_DNS_TYPES = {
    1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT", 28: "AAAA",
    33: "SRV", 64: "SVCB", 65: "HTTPS", 255: "ANY",
}


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


def log_path(root: Path, day: datetime.date) -> Path:
    """The request log the daemon root *root* keeps for *day*."""
    return Path(root) / LOG_DIRNAME / f"{day.isoformat()}.jsonl"


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
    """The request is answered by the proxy itself, with *status*, and goes no further.

    A SOCKS5 client is answered with *reply* instead, the reply code that
    says the same, which follows from the status unless it is given.
    """

    def __init__(self, status: int, message: str, reply: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        if reply is None:
            reply = {REFUSED: _NOT_ALLOWED, 502: _HOST_UNREACHABLE}.get(status, _GENERAL_FAILURE)
        self.reply = reply


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
    ``dial`` unless given. Given the daemon *root*, the proxy logs each
    request, once it ends, as one line of the day's ``log_path`` that names
    the *lease* and *member* it serves.

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
        lease: str | None = None,
        member: str | None = None,
        root: Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.internal = frozenset(_internal(entry) for entry in internal)
        self._resolve = resolver or resolve
        self._dial = dialer or dial
        self._server: asyncio.base_events.Server | None = None
        self._made: tuple[int, int] | None = None
        self._open: set[asyncio.Task] = set()
        self.lease, self.member = lease, member
        self._root = None if root is None else Path(root)

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
        self._server, self._made = await _bound(self.path, self._serve, "web proxy")

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
        _unlink(self.path, self._made)
        self._made = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._open.add(task)
        upstream: asyncio.StreamWriter | None = None
        request: _Request | None = None
        verdict: str | None = None
        sent = received = None
        socks = False
        try:
            try:
                async with asyncio.timeout(_HEAD_SECONDS):
                    first = await _exactly(reader, 1)
                    socks = first[0] == _SOCKS5
                    request = await (_read_socks(reader, writer) if socks else _read(reader, first))
                addresses = await self._checked(request.host, request.port)
                from_upstream, upstream = await self._connect(request, addresses)
            except _Answer as answer:
                verdict = "refused" if answer.status == REFUSED else "failed"
                await (_socks_answer if socks else _answer)(writer, answer)
                return
            verdict = "allowed"
            sent, received = _Counted(upstream), _Counted(writer)
            if socks:
                writer.write(_socks_reply(_SUCCEEDED))
                await writer.drain()
                await _tunnel(reader, received, from_upstream, sent)
            elif request.method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await _tunnel(reader, received, from_upstream, sent)
            else:
                sent.write(request.head)
                await _relay(reader, received, from_upstream, sent, request.length)
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
            self._record(request, verdict, sent, received)

    def _record(self, request: _Request | None, verdict: str | None, sent, received) -> None:
        """Log one request that ended, when the proxy is given a daemon root.

        A client that went away before its request was whole sent none, and
        leaves no line. The line is one small append, made on the loop so it
        is written also when ``stop`` cancels the request; a log that cannot
        be written is said, and the proxy goes on.
        """
        if self._root is None or (request is None and verdict is None):
            return
        _append(self._root, {
            "lease": self.lease,
            "member": self.member,
            "host": None if request is None else request.host,
            "port": None if request is None else request.port,
            "verdict": verdict or "failed",
            "bytes_up": 0 if sent is None else sent.count,
            "bytes_down": 0 if received is None else received.count,
        })

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
        reply = _CONNECTION_REFUSED
        for address in addresses:
            try:
                return await asyncio.wait_for(self._dial(address, request.port), _CONNECT_SECONDS)
            except ConnectionRefusedError:
                continue
            except (OSError, TimeoutError):
                reply = _HOST_UNREACHABLE
                continue
        raise _Answer(
            502, f"{_authority(request.host, request.port)} could not be reached.", reply,
        )


class _Closed(Exception):
    """The client went away before its request was whole."""


class _Counted:
    """A stream writer that counts the bytes written to it."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self.count = 0

    def write(self, data: bytes) -> None:
        self.count += len(data)
        self._writer.write(data)

    def __getattr__(self, name: str):
        return getattr(self._writer, name)


async def _exactly(reader: asyncio.StreamReader, count: int) -> bytes:
    """The next *count* bytes the client sends; raises ``_Closed`` when it goes away first."""
    try:
        return await reader.readexactly(count)
    except asyncio.IncompleteReadError:
        raise _Closed from None


async def _read(reader: asyncio.StreamReader, first: bytes = b"") -> _Request:
    """The request the client sends, read up to its body, *first* being the
    byte of it already read.

    Raises ``_Answer`` for one the proxy does not forward, and ``_Closed``
    when the client goes away first.
    """
    if first and not _TOKEN.match(first.decode("latin-1")):
        # a request line starts with its method, so this is no HTTP request,
        # and neither SOCKS4 nor anything else is waited on for a head
        raise _Answer(400, "The proxy speaks HTTP/1.x and SOCKS5, and this is neither.")
    try:
        raw = first + await reader.readuntil(b"\r\n\r\n")
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


async def _read_socks(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> _Request:
    """The CONNECT a SOCKS5 client asks for, read after the version byte
    that told it from an HTTP one (RFC 1928).

    The proxy takes no authentication and serves CONNECT alone, to an IPv4
    or IPv6 address or a name, which it resolves itself. Raises ``_Answer``
    for anything else, and ``_Closed`` when the client goes away first.
    """
    (count,) = await _exactly(reader, 1)
    methods = await _exactly(reader, count)
    if _NO_AUTH not in methods:
        raise _Answer(400, "The proxy takes no authentication.", _NO_ACCEPTABLE)
    writer.write(bytes((_SOCKS5, _NO_AUTH)))
    await writer.drain()

    version, command, reserved, kind = await _exactly(reader, 4)
    if version != _SOCKS5 or reserved != 0:
        raise _Answer(400, "The request is not a SOCKS5 request.")
    if command != _SOCKS_CONNECT:
        raise _Answer(501, "The proxy serves CONNECT alone.", _COMMAND_NOT_SUPPORTED)
    if kind == _IPV4:
        host = str(ipaddress.IPv4Address(await _exactly(reader, 4)))
    elif kind == _IPV6:
        host = str(ipaddress.IPv6Address(await _exactly(reader, 16)))
    elif kind == _DOMAIN:
        (length,) = await _exactly(reader, 1)
        host = _socks_name(await _exactly(reader, length))
    else:
        raise _Answer(400, "The request's address type is not one SOCKS5 has.", _ADDRESS_NOT_SUPPORTED)
    port = int.from_bytes(await _exactly(reader, 2), "big")
    if not port:
        raise _Answer(400, "The request asks for port 0.")
    return _Request("CONNECT", host, port)


def _socks_name(raw: bytes) -> str:
    """The host a SOCKS5 request names, as ``_host`` gives it.

    Raises ``_Answer`` for one that is not a host name or an address.
    """
    bad = _Answer(400, "The request's host is not a host name.")
    try:
        name = raw.decode("ascii")
    except UnicodeDecodeError:
        raise bad from None
    if not name.isprintable() or " " in name or (":" in name and _literal(name) is None):
        raise bad
    host = _host(name)
    if not host:
        raise bad
    return host


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


def _socks_reply(code: int) -> bytes:
    """A SOCKS5 reply with *code*. It names no bound address: what the proxy
    dialled from is not the member's to know, and a client does not need it."""
    return bytes((_SOCKS5, code, 0x00, _IPV4)) + bytes(6)


async def _socks_answer(writer: asyncio.StreamWriter, answer: _Answer) -> None:
    """Answer a SOCKS5 client with the reply *answer* carries, and nothing else."""
    if answer.reply == _NO_ACCEPTABLE:
        writer.write(bytes((_SOCKS5, _NO_ACCEPTABLE)))
    else:
        writer.write(_socks_reply(answer.reply))
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


def dns_path(proxy_path: Path | str) -> Path:
    """Where the DNS forwarder of the lease whose proxy is served at
    *proxy_path* is served: beside it, under the same name, ending ``.dns``.
    The name is no longer than the proxy's, so a path that holds the one
    holds the other."""
    return Path(proxy_path).with_suffix(DNS_SUFFIX)


def host_nameserver(text: str) -> str:
    """The resolver the host's resolver configuration *text* names first.

    With none named, the C library asks the local host, and so does this.
    """
    for line in text.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] == "nameserver" and _literal(words[1]) is not None:
            return words[1]
    return "127.0.0.1"


class UdpUpstream:
    """The resolver at *address* on *port*, asked over UDP from the loop.

    A call sends one query and answers with the first datagram back that
    carries its id and says it is a response; a datagram from anywhere else
    is not let in, as the socket is connected to the resolver. Raises
    ``TimeoutError`` when none comes within *timeout* seconds, and
    ``OSError`` when the resolver cannot be reached.
    """

    def __init__(self, address: str, port: int = 53, *, timeout: float = _DNS_SECONDS) -> None:
        self.address, self.port, self.timeout = address, port, timeout

    async def __call__(self, query: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        answered: asyncio.Future[bytes] = loop.create_future()
        ident = query[:2]

        class _Answers(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr) -> None:
                if (len(data) >= _DNS_HEADER and data[:2] == ident and data[2] & _QR
                        and not answered.done()):
                    answered.set_result(data)

            def error_received(self, exc: Exception) -> None:
                if not answered.done():
                    answered.set_exception(exc)

        transport, _ = await loop.create_datagram_endpoint(
            _Answers, remote_addr=(self.address, self.port),
        )
        try:
            transport.sendto(query)
            async with asyncio.timeout(self.timeout):
                return await answered
        finally:
            transport.close()


def host_upstream() -> UdpUpstream:
    """The host's own resolver, as its resolver configuration names it."""
    try:
        text = Path(RESOLVER).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return UdpUpstream(host_nameserver(text))


class DnsForwarder:
    """The DNS a web member's sandbox resolves names through, served on a
    unix socket at *path*, on the running loop.

    The sandbox's forwarder takes each UDP query a tool sends to port 53
    inside and makes one connection of it here: the query, then the end of
    it. The query goes to *upstream*, the host's resolver unless given
    (``host_upstream``), and its answer comes back unchanged. The query is
    read only far enough to know it is one plain question and to log its
    name and type; one the forwarder cannot read, or one the upstream does
    not answer, is answered SERVFAIL, and what has no header to answer is
    dropped. Given the daemon *root*, each query leaves one line in the
    proxy's log, with the name as its host, no port, and the record type.

    An answer gives the member an address and no way to it: a connection
    still goes through the proxy, which resolves the name again and checks
    what it dials. ``start`` and ``stop`` are as the proxy's.
    """

    def __init__(
        self,
        path: Path,
        *,
        upstream: DnsUpstream | None = None,
        lease: str | None = None,
        member: str | None = None,
        root: Path | None = None,
    ) -> None:
        self.path = Path(path)
        self._ask = upstream or host_upstream()
        self._server: asyncio.base_events.Server | None = None
        self._made: tuple[int, int] | None = None
        self._open: set[asyncio.Task] = set()
        self.lease, self.member = lease, member
        self._root = None if root is None else Path(root)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def start(self) -> None:
        """Serve the forwarder on the socket at ``path``; raises as the proxy's ``start``."""
        if self._server is not None:
            raise SpecfloError(f"The DNS forwarder at {self.path} is served already.")
        self._server, self._made = await _bound(self.path, self._serve, "DNS forwarder")

    async def stop(self) -> None:
        """Stop serving, end every query still open and remove the socket."""
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        still_open = list(self._open)
        for task in still_open:
            task.cancel()
        await asyncio.gather(*still_open, return_exceptions=True)
        await server.wait_closed()
        _unlink(self.path, self._made)
        self._made = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._open.add(task)
        query = answer = b""
        name = kind = None
        verdict = None
        try:
            async with asyncio.timeout(_HEAD_SECONDS):
                while len(query) <= _DNS_MAX:
                    more = await reader.read(_DNS_MAX + 1 - len(query))
                    if not more:
                        break
                    query += more
            if len(query) < _DNS_HEADER or len(query) > _DNS_MAX:
                # nothing to answer: no id to answer it under, or no datagram
                return
            try:
                name, kind, end = _question(query)
            except ValueError:
                verdict, answer = "failed", _servfail(query, None)
            else:
                try:
                    answer = await asyncio.wait_for(self._ask(query), _DNS_SECONDS)
                    if len(answer) < _DNS_HEADER or answer[:2] != query[:2] or not answer[2] & _QR:
                        raise ValueError("the upstream's answer is not to this query")
                    verdict = "dns"
                except Exception:
                    log.debug("the DNS forwarder's upstream did not answer %s", name, exc_info=True)
                    verdict, answer = "failed", _servfail(query, end)
            writer.write(answer)
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        except Exception:
            log.debug("a DNS query failed", exc_info=True)
        finally:
            writer.close()
            if task is not None:
                self._open.discard(task)
            if self._root is not None and verdict is not None:
                _append(self._root, {
                    "lease": self.lease,
                    "member": self.member,
                    "host": name,
                    "port": None,
                    "type": kind,
                    "verdict": verdict,
                    "bytes_up": len(query),
                    "bytes_down": len(answer),
                })


def _question(query: bytes) -> tuple[str, str, int]:
    """The name and record type *query* asks for, and where its question ends.

    The query must be a standard query that asks one question in class IN
    and carries no answer of its own, and the name must be labels of
    printable ASCII, written out with no pointer. Raises ``ValueError`` for
    anything else.
    """
    questions, answers, authority = struct.unpack("!HHH", query[4:10])
    if query[2] & _QR or (query[2] >> 3) & 0x0F != 0:
        raise ValueError("not a standard query")
    if questions != 1 or answers or authority:
        raise ValueError("not one question alone")
    labels: list[str] = []
    at = _DNS_HEADER
    while True:
        if at >= len(query):
            raise ValueError("the name runs past the query")
        length = query[at]
        if length == 0:
            break
        if length > 63:
            raise ValueError("a label that is a pointer or too long")
        label = query[at + 1 : at + 1 + length]
        if len(label) != length or not all(0x21 <= byte < 0x7F for byte in label):
            raise ValueError("a label that is not printable ASCII")
        labels.append(label.decode("ascii").lower())
        at += 1 + length
    end = at + 5
    if end > len(query):
        raise ValueError("the question is cut short")
    qtype, qclass = struct.unpack("!HH", query[at + 1 : end])
    if qclass != 1:
        raise ValueError("not in class IN")
    name = ".".join(labels) or "."
    if len(name) > 253:
        raise ValueError("the name is too long")
    return name, _DNS_TYPES.get(qtype, f"TYPE{qtype}"), end


def _servfail(query: bytes, end: int | None) -> bytes:
    """SERVFAIL in answer to *query*: its id, opcode and recursion flag, and
    its question when it was read whole, up to *end*."""
    # the query's opcode and recursion flag, kept; recursion available
    flags = (_QR | (query[2] & 0x79)) << 8 | 0x80 | _SERVFAIL
    if end is None:
        return query[:2] + struct.pack("!H4H", flags, 0, 0, 0, 0)
    return query[:2] + struct.pack("!H4H", flags, 1, 0, 0, 0) + query[_DNS_HEADER:end]


async def _bound(path: Path, serve, what: str):
    """A unix server of *serve* on *path*, listening, and the identity of its
    socket; *what* names the server in the errors.

    Raises ``SpecfloError`` for a path too long to bind a socket on or one
    that holds something other than a socket, which is left as it is. A
    socket found at the path is replaced.
    """
    if len(os.fsencode(path)) > _SOCKET_PATH_MAX:
        raise SpecfloError(
            f"The {what} socket path {path} is too long for a unix socket, which "
            f"takes {_SOCKET_PATH_MAX} bytes at most; serve it from a shorter path."
        )
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(found.st_mode):
            raise SpecfloError(
                f"{path} is there and is not a socket; the {what} is served on "
                "a socket at that path, so move what is there out of the way."
            )
    # bound, made owner-only, and only then listening: a socket that does
    # not listen yet refuses every connection
    server = await asyncio.start_unix_server(
        serve, path=str(path), limit=_HEAD_MAX, start_serving=False,
    )
    made = None
    try:
        os.chmod(path, 0o600)
        made = _identity(path)
        await server.start_serving()
    except BaseException:
        server.close()
        _unlink(path, made)
        raise
    return server, made


def _unlink(path: Path, made: tuple[int, int] | None) -> None:
    """Remove the socket at *path*, if what is there is still the one bound as *made*."""
    if made is not None and _identity(path) == made:
        path.unlink()


def _append(root: Path, line: dict) -> None:
    """Add *line*, stamped with the time, to the day's request log under the
    daemon root *root*.

    The line is one small append, made on the loop so it is written also
    when a stop cancels the request; a log that cannot be written is said,
    and the server goes on.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    path = log_path(root, now.date())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as log_file:
            log_file.write(json.dumps({"time": now.isoformat(timespec="seconds"), **line}) + "\n")
    except OSError as exc:
        log.warning("the web proxy could not log a request to %s: %s", path, exc)


def _identity(path: Path) -> tuple[int, int] | None:
    """The device and inode at *path*, None when nothing is there."""
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return None
    return found.st_dev, found.st_ino
