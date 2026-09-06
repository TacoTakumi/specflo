"""The daemon's FastAPI application.

Importing this module needs the ``serve`` extra. :func:`create_app` prepares
the daemon root and returns the application bound to it; the root is kept
on ``app.state.root`` so every route reads and writes under it.

Every route but the health probe depends on :func:`current_identity`: the
bearer token on the request resolves to requester or developer, or the
request is refused with 401 before any route runs.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request

from . import HEALTH_PATH, prepare_root
from .auth import IDENTITIES, identity_for

WHOAMI_PATH = "/whoami"


def current_identity(request: Request) -> str:
    """The identity behind the request's bearer token; 401 without a valid one."""
    scheme, _, secret = request.headers.get("Authorization", "").partition(" ")
    identity = None
    if scheme.lower() == "bearer":
        identity = identity_for(request.app.state.root, secret.strip())
    if identity is None:
        raise HTTPException(
            status_code=401,
            detail="A bearer token bound to one of "
            + " or ".join(IDENTITIES)
            + " is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return identity


def create_app(root: Path) -> FastAPI:
    """The application serving the daemon root at ``root``."""
    root = prepare_root(root)
    app = FastAPI(title="specflo daemon")
    app.state.root = root

    @app.get(HEALTH_PATH)
    def health() -> dict:
        return {"status": "ok"}

    @app.get(WHOAMI_PATH)
    def whoami(identity: str = Depends(current_identity)) -> dict:
        return {"identity": identity}

    return app
