"""The daemon's FastAPI application.

Importing this module needs the ``serve`` extra. :func:`create_app` prepares
the daemon root and returns the application bound to it; the root is kept
on ``app.state.root`` so every route reads and writes under it.

The health probe is the one open route. The API lives on the router in
:mod:`specflo.daemon.routes`, behind the bearer token guard, and the agent
pool's part of it on the router in :mod:`specflo.daemon.pool_routes`; the web
UI in :mod:`specflo.daemon.web` serves its pages behind a browser session.
While a daemon with a pool serves, it reads llama-swap's event stream
(:mod:`specflo.pool.events`).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI

from ..pool import events
from . import HEALTH_PATH, chat, pool_routes, prepare_root, web
from .routes import router


@contextlib.asynccontextmanager
async def _serving(app: FastAPI) -> AsyncIterator[None]:
    """What runs while the daemon serves, and no longer.

    A daemon with a pool reads llama-swap's event stream for the model
    reloads of leased members. The reader is a task on the server's event
    loop, cancelled when the server shuts down; a daemon with no pool, or
    with a pool configuration that did not stand, reads nothing.
    """
    reader = events.reader_for(app.state.pool)
    task = asyncio.create_task(reader.run()) if reader is not None else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app(root: Path, *, url: str | None = None) -> FastAPI:
    """The application serving the daemon root at ``root``.

    ``url`` is where the daemon answers, as its own agents must reach it;
    the seat a project's agent runs in is scaffolded with it.
    """
    root = prepare_root(root)
    # No generated docs or schema routes: those would serve every API path
    # to a browser without a token, and the token guard is the contract.
    app = FastAPI(
        title="specflo daemon", docs_url=None, redoc_url=None, openapi_url=None,
        lifespan=_serving,
    )
    app.state.root = root
    app.state.url = url
    # The chat pumps: one per agent discovery finds alive for a mapped
    # project, so a daemon restart follows every live conversation again.
    app.state.pumps = chat.Pumps(root)
    app.state.pumps.resume()
    # The agent pool the root's pool directory declares; None without one.
    # A directory with faults gives no pool either, and the faults are kept:
    # the projects are served, and the pool routes answer with the faults.
    app.state.pool, app.state.pool_errors = pool_routes.open_pool(root)

    @app.get(HEALTH_PATH)
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(router)
    app.include_router(pool_routes.router)
    web.install(app)
    return app
