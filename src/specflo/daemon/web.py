"""The daemon's web UI: server-rendered pages behind a browser session.

A browser signs in as requester or developer by presenting that identity's
bearer token once, on the sign-in page; the agent identity has no browser,
so the page neither offers it nor accepts its token. The daemon answers with a session
cookie holding a fresh secret of its own; the token itself never reaches the
browser, and the cookie never unlocks the API, whose routes still read only
the Authorization header. Sessions live in the daemon process, so a restart
signs every browser out.

Every page renders a Jinja2 template from the package's ``templates``
directory, from what the store and the project index hold at that moment;
nothing is cached, so a change through the CLI shows on the next request. The browser scripts a page needs ship from the package's
``assets`` directory, which is the one thing served without a session besides
the sign-in page itself: it holds a vendored htmx 4 with its sse extension
(4.0.0, from the htmx.org npm package, Zero-Clause BSD) and nothing is built.
In htmx 4 an extension registers itself when its script loads, so a page
carries no ``hx-ext`` attribute; every attribute and event name a template
uses is 4's, and a structural test scans for 2's.
"""

from __future__ import annotations

import dataclasses
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import load_config
from ..doc import ARTIFACTS
from ..errors import ProjectNotFound, SpecfloError
from ..projects import COMPLETE_STATUS, INITIAL_STATUS, Gate, Project, validate_slug
from ..service.local import LocalProjectService
from ..workflow import PHASES
from .auth import BROWSER_IDENTITIES, identity_for
from . import seat
from .products import Products
from . import chat
from .routes import audit, project_lock
from .store import Product, WorkItem, open_store
from .workitems import FULL_DEV_PATH, WorkItems

HOME_PATH = "/"
SIGNIN_PATH = "/signin"
ASSETS_PATH = "/assets"
PRODUCT_PATH = "/products/{slug}"
PROJECT_PATH = "/projects/{slug}"
TAKE_PATH = "/projects/{slug}/take"
START_AGENT_PATH = "/projects/{slug}/agent/start"
CHAT_PATH = "/projects/{slug}/chat"
START_PROJECT_PATH = "/products/{slug}/items/{item_id}/start"
SESSION_COOKIE = "specflo_session"
# How long a session lives, in seconds; the cookie carries the same limit.
SESSION_TTL = 12 * 60 * 60

# A work item still counts as open work until it is done or dropped.
OPEN_STATUSES = ("open", "in-progress")

# The role each phase waits on when no gate is open. One table, so a later
# slice that hands a phase to the requester changes this line and no template.
WAITING_ROLES: dict[str, str] = {phase: "developer" for phase in PHASES}

SIGNIN_FAILED = "That token does not sign in the identity you chose."
INBOX_EMPTY = "Nothing waits on {identity}."

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
    identities=BROWSER_IDENTITIES,
    product_url=lambda slug: PRODUCT_PATH.format(slug=slug),
    project_url=lambda slug: PROJECT_PATH.format(slug=slug),
    take_url=lambda slug: TAKE_PATH.format(slug=slug),
    start_agent_url=lambda slug: START_AGENT_PATH.format(slug=slug),
    chat_url=lambda slug: CHAT_PATH.format(slug=slug),
    start_project_url=lambda slug, item_id: START_PROJECT_PATH.format(slug=slug, item_id=item_id),
)


class SignInRequired(Exception):
    """A page was asked for by a browser with no live session."""


def _now() -> float:
    return time.time()


def current_session(request: Request) -> str:
    """The identity signed in on the request's cookie; sends to sign-in without one.

    A session is the identity and the moment it expires; an expired one is
    dropped on sight and counts as none.
    """
    secret = request.cookies.get(SESSION_COOKIE, "")
    sessions = request.app.state.sessions
    entry = sessions.get(secret) if secret else None
    if entry is not None and entry[1] <= _now():
        sessions.pop(secret, None)
        entry = None
    if entry is None:
        raise SignInRequired()
    return entry[0]


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
    if (
        identity not in BROWSER_IDENTITIES
        or identity_for(request.app.state.root, token) != identity
    ):
        return render(
            "signin.html", status_code=400, identity=None, error=SIGNIN_FAILED, chosen=identity
        )
    sessions = request.app.state.sessions
    now = _now()
    for stale in [key for key, (_, expires) in sessions.items() if expires <= now]:
        del sessions[stale]
    secret = secrets.token_urlsafe(32)
    sessions[secret] = (identity, now + SESSION_TTL)
    response = RedirectResponse(HOME_PATH, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        secret,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
        secure=request.url.scheme == "https",
    )
    return response


# --- what the pages show ------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ProductCard:
    """One product as the products page lists it."""

    product: Product
    open_items: int
    active_projects: int

    @property
    def summary(self) -> str:
        """The first line of the vision; a product without one has no summary."""
        return self.product.vision.strip().splitlines()[0] if self.product.vision.strip() else ""


def product_cards(root: Path) -> list[ProductCard]:
    """Every product in slug order with its open work and active project counts.

    A project belongs to the product of the work item it was spawned from;
    one made outside any work item belongs to no product and counts for none.
    """
    with open_store(root) as store:
        products = Products(store).list()
        items = WorkItems(store).list()
    projects = LocalProjectService(root, load_config(root)).list_projects()
    product_of_item = {item.id: item.product for item in items}
    active_by_product: dict[str, int] = {}
    for project in projects:
        owner = product_of_item.get(project.work_item)
        if owner is not None and project.status == INITIAL_STATUS:
            active_by_product[owner] = active_by_product.get(owner, 0) + 1
    return [
        ProductCard(
            product=product,
            open_items=sum(
                1
                for item in items
                if item.product == product.slug and item.status in OPEN_STATUSES
            ),
            active_projects=active_by_product.get(product.slug, 0),
        )
        for product in products
    ]


@dataclasses.dataclass(frozen=True)
class InboxEntry:
    """One project waiting on the signed-in role, as the inbox lists it."""

    project: Project
    product: Product | None
    gate: Gate


def inbox_entries(root: Path, identity: str) -> list[InboxEntry]:
    """Every active project whose open gate waits on ``identity``, newest gate first.

    A project belongs to the product of the work item it was spawned from;
    one made outside any work item lists with no product.
    """
    with open_store(root) as store:
        items = {item.id: item for item in WorkItems(store).list()}
        products = {product.slug: product for product in Products(store).list()}
    waiting = []
    for project in LocalProjectService(root, load_config(root)).list_projects():
        gate = project.gate
        if project.status != INITIAL_STATUS or gate is None or not gate.is_open:
            continue
        if gate.role != identity:
            continue
        item = items.get(project.work_item) if project.work_item is not None else None
        product = products.get(item.product) if item is not None else None
        waiting.append(InboxEntry(project=project, product=product, gate=gate))
    waiting.sort(key=lambda entry: entry.gate.opened_at, reverse=True)
    return waiting


@dataclasses.dataclass(frozen=True)
class ProductView:
    """One product as its page shows it.

    ``projects`` are the ones spawned from the product's work items, minus
    the complete ones unless ``show_archived``; ``archived_count`` says how
    many the archive holds either way.
    """

    product: Product
    pieces: list[str]
    items: list[WorkItem]
    projects: list[Project]
    archived_count: int
    show_archived: bool
    # Every owned project's name by slug, archived ones included, so a
    # backlog row names its project even while the archive is folded away.
    project_names: dict[str, str]
    # The items a project can be started on: full dev path, no project yet.
    startable: frozenset[int] = frozenset()


def product_view(root: Path, slug: str, *, show_archived: bool) -> ProductView | None:
    """The page's view of ``slug``, or None for a product the store does not hold."""
    with open_store(root) as store:
        product = store.get_product(slug)
        if product is None:
            return None
        pieces = store.list_pieces(slug)
        items = WorkItems(store).list(product=slug)
    spawned = {item.project for item in items if item.project is not None}
    owned = [
        project
        for project in LocalProjectService(root, load_config(root)).list_projects()
        if project.slug in spawned
    ]
    archived = [project for project in owned if project.status == COMPLETE_STATUS]
    return ProductView(
        product=product,
        pieces=pieces,
        items=items,
        projects=owned if show_archived else [p for p in owned if p not in archived],
        archived_count=len(archived),
        show_archived=show_archived,
        project_names={project.slug: project.name for project in owned},
        startable=frozenset(
            item.id for item in items if item.dev_path == FULL_DEV_PATH and item.project is None
        ),
    )


@dataclasses.dataclass(frozen=True)
class ProjectView:
    """One project as its page shows it.

    ``artifacts`` pairs every artifact name, in pipeline order, with its
    verbatim text, or None for one the project has not created yet.
    ``gate`` is the project's open gate, if it has one and is still active;
    ``waiting_on`` is then the gate's role, otherwise the role the current
    phase waits on. A project that is no longer active waits on nobody.
    ``can_take`` says the viewer is the identity the open gate waits on, so
    the page offers the take. ``agent`` is what discovery says of the
    project's agent, asked on every render, for a project in the phase that
    has one; None for any other, which has no agent section.
    """

    project: Project
    product: Product | None
    waiting_on: str | None
    artifacts: list[tuple[str, str | None]]
    gate: Gate | None = None
    can_take: bool = False
    agent: seat.Liveness | None = None


def has_agent(project: Project) -> bool:
    """Whether the project is one with an agent to show: active, in the chat phase."""
    return project.status == INITIAL_STATUS and project.phase == seat.CHAT_PHASE


def project_view(root: Path, slug: str, *, viewer: str | None = None) -> ProjectView | None:
    """The page's view of ``slug`` as ``viewer`` sees it, or None for a project the daemon does not hold.

    The slug is checked as the API checks it, before it can become a path: a
    string that is not a slug names no project, and no file is read for it. A
    project the daemon holds but cannot read raises: that is a fault of the
    daemon's copy, not a missing page.
    """
    try:
        validate_slug(slug)
    except SpecfloError:
        return None
    service = LocalProjectService(root, load_config(root))
    try:
        project = service.load_project(slug)
    except ProjectNotFound:
        return None
    product = None
    if project.work_item is not None:
        with open_store(root) as store:
            item = store.get_work_item(project.work_item)
            product = store.get_product(item.product) if item is not None else None
    active = project.status == INITIAL_STATUS
    gate = project.gate if active and project.gate is not None and project.gate.is_open else None
    return ProjectView(
        project=project,
        product=product,
        waiting_on=(gate.role if gate else WAITING_ROLES.get(project.phase)) if active else None,
        gate=gate,
        can_take=gate is not None and gate.role == viewer,
        agent=seat.liveness(root, slug) if has_agent(project) else None,
        artifacts=[
            (name, service.show_document(slug, name) if service.has_artifact(slug, name) else None)
            for name in ARTIFACTS
        ],
    )


# --- the pages ----------------------------------------------------------------

# Each page takes the signed-in identity, so a browser without a session is
# sent to sign in before any page renders.
pages = APIRouter()


@pages.get(HOME_PATH, include_in_schema=False)
def products_page(request: Request, identity: str = Depends(current_session)) -> Response:
    root = request.app.state.root
    return render(
        "products.html",
        identity=identity,
        cards=product_cards(root),
        inbox=inbox_entries(root, identity),
        inbox_empty=INBOX_EMPTY.format(identity=identity),
    )


@pages.get(PRODUCT_PATH, include_in_schema=False)
def product_page(
    request: Request,
    slug: str,
    archived: str = "",
    identity: str = Depends(current_session),
) -> Response:
    view = product_view(request.app.state.root, slug, show_archived=archived == "1")
    if view is None:
        return _missing(identity, f"No product {slug!r}.")
    return render("product.html", identity=identity, view=view, session=session_secret(request))


@pages.post(START_PROJECT_PATH, include_in_schema=False)
async def start_project(
    request: Request, slug: str, item_id: int, identity: str = Depends(current_session)
) -> Response:
    """Start a project on the product's work item: spawn, seat, agent, as one operation.

    The form echoes the session secret like every mutating form. The item
    must belong to the product named in the path; what the operation
    refuses (another dev path, a project already spawned) is a 409 page,
    and an agent that fails to start is a 502 page with the project made.
    """
    fields = await form_fields(request)
    if not secrets.compare_digest(fields.get("session", ""), session_secret(request)):
        return render(
            "error.html", status_code=403, identity=identity,
            message="That form did not come from this session.",
        )
    root = request.app.state.root
    with open_store(root) as store:
        item = store.get_work_item(item_id) if store.get_product(slug) is not None else None
    if item is None or item.product != slug:
        return _missing(identity, f"No work item {item_id} on product {slug!r}.")
    url = getattr(request.app.state, "url", None) or seat.DEFAULT_URL
    try:
        started = seat.start_project(root, item_id, identity, url=url)
    except seat.AgentStartError as exc:
        return render("error.html", status_code=502, identity=identity, message=str(exc))
    except SpecfloError as exc:
        return render("error.html", status_code=409, identity=identity, message=str(exc))
    request.app.state.pumps.ensure(started.spawned.project.slug, started.agent)
    return RedirectResponse(PROJECT_PATH.format(slug=started.spawned.project.slug), status_code=303)


# The page takes the rest of the path, not one segment, so a slash smuggled
# into the slug reaches the same slug check and the same 404 page as any
# other string that is not a slug, instead of falling through to the API's
# JSON 404.
@pages.get(PROJECT_PATH.replace("{slug}", "{slug:path}"), include_in_schema=False)
def project_page(
    request: Request, slug: str, identity: str = Depends(current_session)
) -> Response:
    view = project_view(request.app.state.root, slug, viewer=identity)
    if view is None:
        return _missing(identity, f"No project {slug!r}.")
    return render(
        "project.html", identity=identity, view=view, session=session_secret(request)
    )


def session_secret(request: Request) -> str:
    """The live session's cookie value: the secret a mutating form must echo back."""
    return request.cookies.get(SESSION_COOKIE, "")


async def form_fields(request: Request) -> dict[str, str]:
    """The URL-encoded fields a form posted, first value each."""
    form = parse_qs((await request.body()).decode(errors="replace"), keep_blank_values=True)
    return {name: values[0] for name, values in form.items()}


@pages.post(TAKE_PATH, include_in_schema=False)
async def take_gate(
    request: Request, slug: str, identity: str = Depends(current_session)
) -> Response:
    """Take the project's open gate as the signed-in identity.

    The form echoes the session secret; a post without it is a cross-site
    post and is refused before anything is read. The take runs through the
    same operation as the CLI verb, under the project's lock, and is
    audited like a take over the API.
    """
    fields = await form_fields(request)
    if not secrets.compare_digest(fields.get("session", ""), session_secret(request)):
        return render(
            "error.html", status_code=403, identity=identity,
            message="That form did not come from this session.",
        )
    root = request.app.state.root
    view = project_view(root, slug, viewer=identity)
    if view is None:
        return _missing(identity, f"No project {slug!r}.")
    if view.gate is not None and not view.can_take:
        return render(
            "error.html", status_code=403, identity=identity,
            message=f"This gate waits on {view.gate.role}, not {identity}.",
        )
    service = LocalProjectService(root, load_config(root), actor=identity, hosted=True)
    with project_lock(root, slug):
        try:
            project = service.take_gate(slug)
        except SpecfloError as exc:
            return render("error.html", status_code=409, identity=identity, message=str(exc))
        audit(root, identity, "take_gate", slug, None)
        seat.announce_take(root, project)
    return RedirectResponse(PROJECT_PATH.format(slug=slug), status_code=303)


@pages.post(START_AGENT_PATH, include_in_schema=False)
async def start_agent(
    request: Request, slug: str, identity: str = Depends(current_session)
) -> Response:
    """Start the project's agent again, for a project whose agent is dead or was never started.

    The form echoes the session secret like every mutating form. The
    project must be one with an agent to show; one whose agent discovery
    finds serving is refused, as a 409 page, so the control cannot start a
    second agent beside it. A start that fails is a 502 page.
    """
    fields = await form_fields(request)
    if not secrets.compare_digest(fields.get("session", ""), session_secret(request)):
        return render(
            "error.html", status_code=403, identity=identity,
            message="That form did not come from this session.",
        )
    root = request.app.state.root
    view = project_view(root, slug, viewer=identity)
    if view is None:
        return _missing(identity, f"No project {slug!r}.")
    if view.agent is None:
        return render(
            "error.html", status_code=409, identity=identity,
            message=f"Project {slug!r} is past the {seat.CHAT_PHASE} phase and has no agent to start.",
        )
    url = getattr(request.app.state, "url", None) or seat.DEFAULT_URL
    try:
        name = seat.start_project_agent(root, slug, identity, url=url)
    except seat.AgentStartError as exc:
        return render("error.html", status_code=502, identity=identity, message=str(exc))
    except SpecfloError as exc:
        return render("error.html", status_code=409, identity=identity, message=str(exc))
    request.app.state.pumps.ensure(slug, name)
    return RedirectResponse(PROJECT_PATH.format(slug=slug), status_code=303)


@pages.post(CHAT_PATH, include_in_schema=False)
async def post_message(
    request: Request, slug: str, identity: str = Depends(current_session)
) -> Response:
    """Send the signed-in identity's message to the project's agent.

    The form echoes the session secret like every mutating form. The text
    goes to the agent prefixed with the identity's label; an agent mid-run
    takes it as a steer, and the post returns once the agent has the
    prompt, not when the run settles. A project past the phase that has an
    agent is a 409 page; an agent that does not serve, or refuses, is a
    502 page; a blank message is a 400 page and reaches no socket.
    """
    fields = await form_fields(request)
    if not secrets.compare_digest(fields.get("session", ""), session_secret(request)):
        return render(
            "error.html", status_code=403, identity=identity,
            message="That form did not come from this session.",
        )
    root = request.app.state.root
    view = project_view(root, slug, viewer=identity)
    if view is None:
        return _missing(identity, f"No project {slug!r}.")
    if view.agent is None:
        return render(
            "error.html", status_code=409, identity=identity,
            message=f"Project {slug!r} is past the {seat.CHAT_PHASE} phase and has no agent to talk to.",
        )
    text = fields.get("text", "").strip()
    if not text:
        return render(
            "error.html", status_code=400, identity=identity, message="The message is empty.",
        )
    try:
        chat.post_message(root, slug, identity, text)
    except seat.AgentMessageError as exc:
        return render("error.html", status_code=502, identity=identity, message=str(exc))
    return RedirectResponse(PROJECT_PATH.format(slug=slug), status_code=303)


def _missing(identity: str, message: str) -> HTMLResponse:
    """A 404 as a page of the UI, not the API's JSON."""
    return render("missing.html", status_code=404, identity=identity, message=message)


def _to_error_page(request: Request, exc: SpecfloError) -> HTMLResponse:
    """A refusal raised while a page rendered: a 500 as a page of the UI, never a traceback."""
    try:
        identity = current_session(request)
    except SignInRequired:
        identity = None
    return render("error.html", status_code=500, identity=identity, message=str(exc))


def install(app: FastAPI) -> None:
    """Add the web UI to ``app``: its pages, the sign-in routes, and the assets."""
    app.state.sessions = {}
    app.add_exception_handler(SignInRequired, _to_sign_in)
    app.add_exception_handler(SpecfloError, _to_error_page)
    app.include_router(front_door)
    app.include_router(pages)
    app.mount(ASSETS_PATH, StaticFiles(directory=ASSETS_DIR), name="assets")
