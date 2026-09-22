"""The socket the daemon serves the bridge filter on: made, served and taken down.

A local member reaches llama-swap through one socket per llama-swap, which
the daemon makes when it starts and removes when it stops. The socket is the
operator's alone, it answers with the filter and nothing else, and a second
daemon started on the same path leaves the first one's socket where it is
rather than binding over it: a socket unlinked from under a running server
still listens, on a name nobody can reach any more.

Nothing here reaches the rig: the upstream is a mock transport in this
process, and the daemon's own llama-swap address is a closed port.
"""

from __future__ import annotations

import asyncio
import os
import socket
import stat
from pathlib import Path

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from specflo.daemon import HEALTH_PATH
from specflo.daemon import app as daemon_app
from specflo.errors import SpecfloError
from specflo.pool import bridge
from specflo.pool.cli_admin import pool_dir


def upstream() -> httpx.AsyncClient:
    """A llama-swap that answers every request with the path it was asked for."""
    async def answer(scope, receive, send) -> None:
        if scope["type"] == "http":
            await JSONResponse({"served": scope["path"]})(scope, receive, send)

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=answer), base_url="http://llama-swap"
    )


def over(path: Path) -> httpx.AsyncClient:
    """A client that speaks HTTP over the unix socket at *path*."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=str(path)), base_url="http://bridge"
    )


def identity(path: Path) -> tuple[int, int]:
    found = os.stat(path)
    return found.st_dev, found.st_ino


def stale_socket(path: Path) -> None:
    """Leave a socket at *path* that nothing listens on, as a daemon killed
    without its shutdown leaves one."""
    left = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    left.bind(str(path))
    left.close()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "llama-swap.sock"


def test_the_socket_is_made_owner_only_serves_the_filter_and_is_removed(path):
    async def run() -> None:
        async with bridge.serving(path, "http://llama-swap", upstream()):
            mode = os.stat(path).st_mode
            assert stat.S_ISSOCK(mode)
            assert stat.S_IMODE(mode) == 0o600
            async with over(path) as client:
                allowed = await client.get("/v1/models")
                refused = await client.get("/logs/stream")
            assert allowed.status_code == 200
            assert allowed.json() == {"served": "/v1/models"}
            assert refused.status_code == bridge.REFUSED
        assert not path.exists()

    asyncio.run(run())


def test_a_second_start_on_the_same_path_leaves_the_first_socket_serving(path):
    async def run() -> None:
        async with bridge.serving(path, "http://llama-swap", upstream()):
            first = identity(path)
            with pytest.raises(bridge.BridgeTaken) as refused:
                async with bridge.serving(path, "http://llama-swap", upstream()):
                    pass
            assert str(path) in str(refused.value)
            assert identity(path) == first
            async with over(path) as client:
                assert (await client.get("/v1/models")).status_code == 200
        assert not path.exists()

    asyncio.run(run())


def test_a_socket_left_by_a_daemon_that_died_is_replaced(path):
    stale_socket(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        with pytest.raises(ConnectionRefusedError):
            probe.connect(str(path))

    async def run() -> None:
        async with bridge.serving(path, "http://llama-swap", upstream()):
            async with over(path) as client:
                assert (await client.get("/v1/models")).status_code == 200

    asyncio.run(run())


def test_a_start_after_a_clean_stop_serves_again(path):
    async def run() -> None:
        for _ in range(2):
            async with bridge.serving(path, "http://llama-swap", upstream()):
                async with over(path) as client:
                    assert (await client.get("/v1/models")).status_code == 200
            assert not path.exists()

    asyncio.run(run())


def test_what_is_not_a_socket_at_the_path_is_refused_and_left(path):
    path.write_text("the operator's\n", encoding="utf-8")

    async def run() -> None:
        async with bridge.serving(path, "http://llama-swap", upstream()):
            pass

    with pytest.raises(SpecfloError, match="not a socket"):
        asyncio.run(run())
    assert path.read_text(encoding="utf-8") == "the operator's\n"


def test_a_path_too_long_for_a_unix_socket_is_refused_by_name(tmp_path):
    deep = tmp_path / ("d" * 120) / "llama-swap.sock"
    deep.parent.mkdir()

    async def run() -> None:
        async with bridge.serving(deep, "http://llama-swap", upstream()):
            pass

    with pytest.raises(SpecfloError, match="too long"):
        asyncio.run(run())
    assert not deep.exists()


def test_the_daemon_serves_the_socket_while_it_serves_a_pool_and_removes_it(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    pool_dir(root).mkdir()
    socket_path = bridge.socket_path(root)

    with TestClient(daemon_app.create_app(root)) as client:
        assert client.get(HEALTH_PATH).status_code == 200
        assert stat.S_ISSOCK(os.stat(socket_path).st_mode)
        assert stat.S_IMODE(os.stat(socket_path).st_mode) == 0o600
        # A second daemon on the same root does not start, and the first
        # one's socket is still the one at the path.
        first = identity(socket_path)
        with pytest.raises(bridge.BridgeTaken):
            with TestClient(daemon_app.create_app(root)):
                pass
        assert identity(socket_path) == first

    assert not socket_path.exists()


def test_a_daemon_with_no_pool_makes_no_socket(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    with TestClient(daemon_app.create_app(root)) as client:
        assert client.get(HEALTH_PATH).status_code == 200
        assert not bridge.socket_path(root).exists()
