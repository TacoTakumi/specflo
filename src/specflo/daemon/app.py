"""The daemon's FastAPI application.

Importing this module needs the ``serve`` extra. :func:`create_app` prepares
the daemon root and returns the application bound to it; the root is kept
on ``app.state.root`` so every route reads and writes under it.

The health probe is the one open route. The API lives on the router in
:mod:`specflo.daemon.routes`, behind the bearer token guard; the web UI in
:mod:`specflo.daemon.web` serves its pages behind a browser session.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from . import HEALTH_PATH, prepare_root, web
from .routes import router


def create_app(root: Path) -> FastAPI:
    """The application serving the daemon root at ``root``."""
    root = prepare_root(root)
    # No generated docs or schema routes: those would serve every API path
    # to a browser without a token, and the token guard is the contract.
    app = FastAPI(title="specflo daemon", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.root = root

    @app.get(HEALTH_PATH)
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(router)
    web.install(app)
    return app
