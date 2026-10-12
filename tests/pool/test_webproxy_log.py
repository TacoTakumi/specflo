"""The web proxy's request log: one JSON line per request, in a file per day.

A proxy served for a lease is told the lease, the member and the daemon root.
Each request it reads leaves one line, once the request ends, in the file
for that day under the daemon root: when it ended, the lease and member, the
host and port the request named, whether it was let through, and how many
bytes went up to the host and came down from it. A proxy that is not told a
daemon root writes no log.

Nothing here leaves this machine: the resolver and dialer are the stubs the
proxy's own tests use, and the upstream is served in this process.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path

from specflo.pool import webproxy

from .test_webproxy import (
    GREETING,
    NO_AUTH,
    PUBLIC,
    PUBLIC_NAME,
    StubDialer,
    StubResolver,
    Upstream,
    answer_head,
    exchange,
    path,
    short_dir,
    socks_connect,
    socks_open,
    socks_reply,
    status,
)

__all__ = ["path", "short_dir"]  # the fixtures, imported for their use here

LEASE = "lease-0001"
MEMBER = "researcher"
FIELDS = {"time", "lease", "member", "host", "port", "verdict", "bytes_up", "bytes_down"}


def today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


def lines(root: Path) -> list[dict]:
    """Every line of the request log under *root*, from the one file it holds."""
    days = sorted(webproxy.log_path(root, today()).parent.iterdir())
    assert len(days) == 1, days
    assert days[0] == webproxy.log_path(root, today())
    return [json.loads(line) for line in days[0].read_text(encoding="utf-8").splitlines()]


def leased(path: Path, root: Path, **stubs) -> webproxy.WebProxy:
    return webproxy.WebProxy(path, lease=LEASE, member=MEMBER, root=root, **stubs)


def test_an_allowed_and_a_refused_request_leave_one_line_each(path, short_dir):
    root = short_dir / "daemon"
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC], "inside.example": ["10.0.0.5"]})

    async def run() -> tuple[bytes, str]:
        async with Upstream() as upstream:
            async with leased(path, root, resolver=resolver, dialer=StubDialer(upstream.address)):
                allowed = await exchange(
                    path, b"GET http://public.example/a?b=c HTTP/1.1\r\nHost: public.example\r\n\r\n",
                )
                refused = await exchange(
                    path, b"CONNECT inside.example:443 HTTP/1.1\r\nHost: inside.example:443\r\n\r\n",
                )
            assert status(allowed) == 200 and status(refused) == 403
            return allowed, upstream.heads[0]

    allowed, sent_up = asyncio.run(run())
    first, second = lines(root)
    assert set(first) == FIELDS and set(second) == FIELDS
    assert first == {
        "time": first["time"], "lease": LEASE, "member": MEMBER,
        "host": PUBLIC_NAME, "port": 80, "verdict": "allowed",
        "bytes_up": len(sent_up), "bytes_down": len(allowed),
    }
    assert second == {
        "time": second["time"], "lease": LEASE, "member": MEMBER,
        "host": "inside.example", "port": 443, "verdict": "refused",
        "bytes_up": 0, "bytes_down": 0,
    }
    for line in (first, second):
        ended = datetime.datetime.fromisoformat(line["time"])
        assert ended.utcoffset() == datetime.timedelta(0)
        assert ended.date() == today()


def test_a_tunnel_counts_the_bytes_each_way(path, short_dir):
    root = short_dir / "daemon"
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC]})

    async def run() -> tuple[bytes, bytes]:
        async with Upstream() as upstream:
            async with leased(path, root, resolver=resolver, dialer=StubDialer(upstream.address)):
                reader, writer = await asyncio.open_unix_connection(str(path))
                try:
                    writer.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
                    await writer.drain()
                    await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                    request = b"GET / HTTP/1.1\r\nHost: public.example\r\n\r\n"
                    writer.write(request)
                    await writer.drain()
                    answer = await asyncio.wait_for(reader.read(), 10)
                finally:
                    writer.close()
        return request, answer

    request, answer = asyncio.run(run())
    (line,) = lines(root)
    assert (line["host"], line["port"], line["verdict"]) == (PUBLIC_NAME, 443, "allowed")
    assert (line["bytes_up"], line["bytes_down"]) == (len(request), len(answer))


def test_a_request_that_fails_is_logged_as_failed(path, short_dir):
    root = short_dir / "daemon"

    async def run() -> None:
        async with leased(path, root, resolver=StubResolver({PUBLIC_NAME: [PUBLIC]}), dialer=StubDialer()):
            answer = await answer_head(path, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
            assert status(answer) == 502

    asyncio.run(run())
    (line,) = lines(root)
    assert (line["host"], line["verdict"], line["bytes_up"], line["bytes_down"]) == (
        PUBLIC_NAME, "failed", 0, 0,
    )


def test_a_proxy_without_a_daemon_root_writes_no_log(path, short_dir):
    async def run() -> None:
        proxy = webproxy.WebProxy(path, resolver=StubResolver({PUBLIC_NAME: [PUBLIC]}), dialer=StubDialer())
        async with proxy:
            await answer_head(path, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")

    asyncio.run(run())
    assert sorted(each.name for each in short_dir.iterdir()) == []


def test_a_log_that_cannot_be_written_does_not_stop_the_proxy(path, short_dir):
    root = short_dir / "daemon"
    root.write_text("not a directory")
    resolver = StubResolver({"inside.example": ["10.0.0.5"]})

    async def run() -> list[int]:
        async with leased(path, root, resolver=resolver, dialer=StubDialer()):
            first = await exchange(path, b"CONNECT inside.example:443 HTTP/1.1\r\n\r\n")
            second = await exchange(path, b"CONNECT inside.example:443 HTTP/1.1\r\n\r\n")
        return [status(first), status(second)]

    assert asyncio.run(run()) == [403, 403]


def test_a_socks_request_leaves_one_line_with_the_same_fields(path, short_dir):
    root = short_dir / "daemon"
    resolver = StubResolver({PUBLIC_NAME: [PUBLIC], "inside.example": ["10.0.0.5"]})

    async def run() -> tuple[bytes, bytes, bytes]:
        async with Upstream() as upstream:
            async with leased(path, root, resolver=resolver, dialer=StubDialer(upstream.address)):
                _, reply, reader, writer = await socks_open(path, PUBLIC_NAME, 443)
                try:
                    request = b"GET / HTTP/1.1\r\nHost: public.example\r\n\r\n"
                    writer.write(request)
                    await writer.drain()
                    answer = await asyncio.wait_for(reader.read(), 10)
                finally:
                    writer.close()
                refused = await exchange(path, GREETING + socks_connect("inside.example", 443))
                failed = await exchange(path, GREETING + socks_connect(PUBLIC_NAME, 443, command=0x02))
        assert reply == socks_reply(0x00)
        assert refused == NO_AUTH + socks_reply(0x02)
        assert failed == NO_AUTH + socks_reply(0x07)
        return request, answer

    request, answer = asyncio.run(run())
    # the tunnel's line is written when it ends, which may be after the others
    every = lines(root)
    found = {line["verdict"]: line for line in every}
    assert len(every) == len(found) == 3
    assert all(set(line) == FIELDS for line in every)
    allowed, refused, failed = found["allowed"], found["refused"], found["failed"]
    assert allowed == {
        "time": allowed["time"], "lease": LEASE, "member": MEMBER,
        "host": PUBLIC_NAME, "port": 443, "verdict": "allowed",
        "bytes_up": len(request), "bytes_down": len(answer),
    }
    assert refused == {
        "time": refused["time"], "lease": LEASE, "member": MEMBER,
        "host": "inside.example", "port": 443, "verdict": "refused",
        "bytes_up": 0, "bytes_down": 0,
    }
    assert (failed["verdict"], failed["bytes_up"], failed["bytes_down"]) == ("failed", 0, 0)
