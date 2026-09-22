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
"""

from __future__ import annotations

import posixpath
from collections.abc import AsyncIterator, Iterable

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

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

    A client the caller gave is the caller's to close, and so is the socket
    the app is served on: what a running bridge is made of and taken down
    with is not this module's.
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
