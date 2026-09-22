"""The host side of a local member's bridge to llama-swap: what it may ask for.

A local member has no route off this host and reaches the rig's llama-swap
through a socket the daemon owns. The daemon does not forward what arrives on
that socket: it reads the request line and answers all but a short list
itself.

llama-swap serves far more than completions. Its log stream carries every
other member's activity through the one server they share, its unload path
lets a member evict a model another member is holding, and its user
interface, its running list and its version endpoint say what else the rig is
doing. None of that is a member's business, and all of it stays inside this
host either way, so the class of a local member is not what is at stake: one
member reading or disturbing another is.

So the filter is an allow list of three: a chat completion, a completion and
the models listing, each on the one method it is made with. Everything else
is refused, whether or not it exists upstream and whether or not llama-swap
grows a new endpoint tomorrow. A path is matched after it is normalized, so
nothing reaches an allowed name by walking up from another.

The filter is the boundary itself, not a convenience: the forwarder inside a
member's sandbox is the member's to kill, and a member that speaks to the
bound socket with a client of its own arrives right here.

The daemon serves the filter on one unix socket per llama-swap, in its root,
for as long as it serves a pool (``serving``). The socket is the operator's
alone, and it is made so before it listens, so no one else connects to it
even for a moment. Whoever serves the socket holds a lock beside it for as
long as it does. A second daemon started on the same root does not get the
lock and does not start: it would otherwise unlink the first one's socket
and bind its own, and the first would go on listening on a name nobody can
reach. A socket found at the path under a lock nobody holds was left by a
daemon that died, and is replaced.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import posixpath
import socket
import stat
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import httpx
import uvicorn
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..errors import SpecfloError

# The method and path of each request a member may make. A member asks for a
# completion and asks what models it may name, and nothing else.
ALLOWED: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/completions"),
    ("GET", "/v1/models"),
})

# What a refused request is answered with. It is not 404: the endpoint is
# there and the member may not have it, and a member that reads the answer
# should not go looking for the path it spelled wrong.
REFUSED = 403

# Headers that belong to one hop and are never passed on to the next.
_HOP_BY_HOP: frozenset[str] = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})

# Set by whatever serves the response, and wrong for a body that is streamed
# through: the length upstream declared is not this response's to promise.
_REWRITTEN: frozenset[str] = _HOP_BY_HOP | {"content-length"}

# The socket's name in the daemon root. A daemon reaches one llama-swap, so
# there is one.
SOCKET_NAME = "llama-swap.sock"

# The longest path a unix socket may be bound on: the kernel's field holds
# 108 bytes, the last of them the terminating zero.
_SOCKET_PATH_MAX = 107

# How long a completion still streaming when the daemon stops is given to
# finish, in seconds, before it is cut off.
_SHUTDOWN_GRACE = 5


class BridgeTaken(SpecfloError):
    """Another process serves the bridge at the path."""


def allowed(method: str, path: str) -> bool:
    """Whether a member may make this request of llama-swap.

    The path is normalized first: ``/v1/models/../../logs/stream`` names the
    log stream, and a filter that matched the text would pass it. A trailing
    slash is not a different endpoint.
    """
    return (method.upper(), _normalized(path)) in ALLOWED


def _normalized(path: str) -> str:
    """*path* with its traversals resolved and a trailing slash dropped."""
    resolved = posixpath.normpath("/" + path.lstrip("/"))
    return resolved.rstrip("/") or "/"


def _passed(headers: Iterable[tuple[str, str]], drop: frozenset[str]) -> list[tuple[str, str]]:
    return [(name, value) for name, value in headers if name.lower() not in drop]


def filter_app(upstream: str, client: httpx.AsyncClient | None = None) -> ASGIApp:
    """An app that answers for llama-swap at *upstream*, allow list first.

    *client* is the client the allowed requests go out on; one is made for
    *upstream* when none is given. A request is streamed both ways, so a
    completion that arrives token by token reaches the member that way.

    There is no route table: every request of every method arrives at the
    one decision below, so a method nobody thought of is refused there
    rather than answered by a router with a status of its own.
    """
    ours = client is None
    out = httpx.AsyncClient(base_url=upstream) if ours else client

    async def handle(request: Request) -> Response:
        path = request.url.path
        if not allowed(request.method, path):
            return JSONResponse(
                {"error": {
                    "message": (
                        f"'{request.method} {_normalized(path)}' is not one of the "
                        "requests a pooled member may make of llama-swap."
                    ),
                    "type": "specflo_pool_refused",
                }},
                status_code=REFUSED,
            )
        upward = out.build_request(
            request.method,
            httpx.URL(url=_normalized(path), query=request.url.query.encode("ascii")),
            headers=_passed(request.headers.items(), _HOP_BY_HOP | {"host"}),
            content=_body(request),
        )
        answer = await out.send(upward, stream=True)
        return StreamingResponse(
            answer.aiter_raw(),
            status_code=answer.status_code,
            headers=dict(_passed(answer.headers.items(), _REWRITTEN)),
            background=BackgroundTask(answer.aclose),
        )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await _lifespan(receive, send, out if ours else None)
        elif scope["type"] == "http":
            answer = await handle(Request(scope, receive))
            await answer(scope, receive, send)

    return app


async def _lifespan(receive: Receive, send: Send, close: httpx.AsyncClient | None) -> None:
    """Answer the server's startup and shutdown, closing a client of our own.

    A client the caller gave is the caller's to close.
    """
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            if close is not None:
                await close.aclose()
            await send({"type": "lifespan.shutdown.complete"})
            return


async def _body(request: Request) -> AsyncIterator[bytes]:
    """The request's body as it arrives, so nothing of it is held here."""
    async for chunk in request.stream():
        yield chunk


def socket_path(root: Path) -> Path:
    """The bridge socket of the daemon root *root*."""
    return Path(root) / SOCKET_NAME


@contextlib.asynccontextmanager
async def serving(
    path: Path, upstream: str, client: httpx.AsyncClient | None = None
) -> AsyncIterator[None]:
    """Serve the filter for llama-swap at *upstream* on a unix socket at
    *path* while the block runs, and remove the socket after it.

    *client* is handed to ``filter_app`` as it is. The server runs on the
    running event loop and leaves the process's signals alone: they are the
    daemon's, and the daemon ends the block when it stops.

    Raises ``BridgeTaken`` when another process serves *path*, and
    ``SpecfloError`` for a path too long to bind a socket on or one that
    holds something other than a socket. Nothing at the path is touched
    either way.
    """
    path = Path(path)
    if len(os.fsencode(path)) > _SOCKET_PATH_MAX:
        raise SpecfloError(
            f"The bridge socket path {path} is too long for a unix socket, which takes "
            f"{_SOCKET_PATH_MAX} bytes at most; give the daemon a root with a shorter path."
        )
    with _held(path):
        listener = _bind(path)
        made = _identity(path)
        server = _Server(uvicorn.Config(
            filter_app(upstream, client),
            lifespan="on",
            # the daemon's logging is configured already, and stays as it is
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=_SHUTDOWN_GRACE,
        ))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if task.done():
                    task.result()
                    raise SpecfloError(f"The bridge at {path} stopped before it served.")
                await asyncio.sleep(0.01)
            yield
        finally:
            server.should_exit = True
            try:
                await task
            finally:
                listener.close()
                if _identity(path) == made:
                    path.unlink()


class _Server(uvicorn.Server):
    """A server that leaves the process's signals to the daemon's own server."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


@contextlib.contextmanager
def _held(path: Path):
    """Hold the lock beside *path* for as long as the block runs.

    The lock goes with the process: a daemon that dies leaves the lock free
    and its socket behind, and the next start replaces the socket.
    """
    descriptor = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeTaken(
                f"Another process serves the bridge at {path}: a second daemon on this "
                "root does not start. Stop the one that runs, or give this one another root."
            ) from None
        yield
    finally:
        os.close(descriptor)


def _bind(path: Path) -> socket.socket:
    """A listening socket at *path*, the operator's alone.

    It is made owner-only between the bind and the listen, since a socket
    that does not listen yet refuses every connection. What is at the path
    already is replaced only if it is a socket: under the lock, one was left
    by a daemon that died.
    """
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(found.st_mode):
            raise SpecfloError(
                f"{path} is there and is not a socket; the bridge is served on a socket at "
                "that path, so move what is there out of the way."
            )
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen()
    except BaseException:
        listener.close()
        path.unlink(missing_ok=True)
        raise
    return listener


def _identity(path: Path) -> tuple[int, int] | None:
    """The device and inode at *path*, None when nothing is there."""
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return None
    return found.st_dev, found.st_ino
