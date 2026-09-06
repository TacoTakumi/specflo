"""The daemon's web UI: server-rendered pages behind a browser session.

A browser signs in as one of the two identities by presenting that identity's
bearer token once, on the sign-in page. The daemon answers with a session
cookie holding a fresh secret of its own; the token itself never reaches the
browser, and the cookie never unlocks the API, whose routes still read only
the Authorization header. Sessions live in the daemon process, so a restart
signs every browser out.

Every page renders a Jinja2 template from the package's ``templates``
directory. The browser scripts a page needs ship from the package's
``assets`` directory, which is the one thing served without a session besides
the sign-in page itself: it holds a vendored htmx (2.0.10, from the htmx.org
npm package, Zero-Clause BSD) and nothing is built.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .auth import IDENTITIES, identity_for

HOME_PATH = "/"
SIGNIN_PATH = "/signin"
ASSETS_PATH = "/assets"
SESSION_COOKIE = "specflo_session"

SIGNIN_FAILED = "That token does not sign in the identity you chose."

TEMPLATES_DIR = Path(__file__).parent / "templates"
ASSETS_DIR = Path(__file__).parent / "assets"

_templates = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)
_templates.globals.update(
    home_path=HOME_PATH,
    signin_path=SIGNIN_PATH,
    assets_path=ASSETS_PATH,
    identities=IDENTITIES,
)


class SignInRequired(Exception):
    """A page was asked for by a browser with no live session."""


def current_session(request: Request) -> str:
    """The identity signed in on the request's cookie; sends to sign-in without one."""
    secret = request.cookies.get(SESSION_COOKIE, "")
    identity = request.app.state.sessions.get(secret) if secret else None
    if identity is None:
        raise SignInRequired()
    return identity


def render(template: str, status_code: int = 200, **context) -> HTMLResponse:
    """The page ``template`` renders with ``context``."""
    html = _templates.get_template(template).render(**context)
    return HTMLResponse(html, status_code=status_code)


def _to_sign_in(request: Request, exc: SignInRequired) -> RedirectResponse:
    return RedirectResponse(SIGNIN_PATH, status_code=303)


# The routes a browser reaches without a session.
front_door = APIRouter()


@front_door.get(SIGNIN_PATH, include_in_schema=False)
def sign_in_page(request: Request) -> Response:
    try:
        current_session(request)
    except SignInRequired:
        return render("signin.html", identity=None)
    return RedirectResponse(HOME_PATH, status_code=303)


@front_door.post(SIGNIN_PATH, include_in_schema=False)
async def sign_in(request: Request) -> Response:
    # The form posts URL-encoded fields; the standard library parses those,
    # so form handling pulls in no further package.
    form = parse_qs((await request.body()).decode(errors="replace"), keep_blank_values=True)
    identity = form.get("identity", [""])[0]
    token = form.get("token", [""])[0].strip()
    if identity not in IDENTITIES or identity_for(request.app.state.root, token) != identity:
        return render(
            "signin.html", status_code=400, identity=None, error=SIGNIN_FAILED, chosen=identity
        )
    secret = secrets.token_urlsafe(32)
    request.app.state.sessions[secret] = identity
    response = RedirectResponse(HOME_PATH, status_code=303)
    response.set_cookie(SESSION_COOKIE, secret, httponly=True, samesite="lax", path="/")
    return response


# The pages; each takes the signed-in identity, so a browser without a
# session is sent to sign in before any page renders.
pages = APIRouter()


@pages.get(HOME_PATH, include_in_schema=False)
def home(identity: str = Depends(current_session)) -> Response:
    return render("base.html", identity=identity)


def install(app: FastAPI) -> None:
    """Add the web UI to ``app``: its pages, the sign-in routes, and the assets."""
    app.state.sessions = {}
    app.add_exception_handler(SignInRequired, _to_sign_in)
    app.include_router(front_door)
    app.include_router(pages)
    app.mount(ASSETS_PATH, StaticFiles(directory=ASSETS_DIR), name="assets")
