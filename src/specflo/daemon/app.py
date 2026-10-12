"""The daemon's FastAPI application.

Importing this module needs the ``serve`` extra. :func:`create_app` prepares
the daemon root and returns the application bound to it; the root is kept
on ``app.state.root`` so every route reads and writes under it.

The health probe is the one open route. The API lives on the router in
:mod:`specflo.daemon.routes`, behind the bearer token guard, and the agent
pool's part of it on the router in :mod:`specflo.daemon.pool_routes`; the web
UI in :mod:`specflo.daemon.web` serves its pages behind a browser session.
While a daemon with a pool serves, it reads llama-swap's event stream
(:mod:`specflo.pool.events`), the event logs of its leased hosted members
(:mod:`specflo.pool.watch`) and the keys of its declared accounts
(:mod:`specflo.pool.accounts`), and serves the bridge its local members reach
llama-swap through (:mod:`specflo.pool.bridge`) and the web proxy of each
lease whose local member has web access (:mod:`specflo.pool.webproxy`). A
daemon whose root has a gateway configuration also runs the issue gateway
(:mod:`specflo.gateway`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path

from fastapi import FastAPI

from .. import gateway
from ..config import proxy_auth
from ..errors import SpecfloError
from ..gateway.config import open_gateway
from ..gateway.run import ServicePool
from ..gateway.service import GatewayService
from ..gateway.store import open_store
from ..pool import accounts, bridge, events, service, watch, webproxy
from ..pool.cli_admin import pool_dir
from . import HEALTH_PATH, auth, chat, pool_routes, prepare_root, web
from .routes import router

_log = logging.getLogger(__name__)

# How long a grant or an ending waits for the daemon's loop to start or stop a
# lease's web proxy, in seconds. Either takes a moment; a loop that does not
# get to it in this time is stuck.
_WEB_SECONDS = 30.0


class WebProxies:
    """The web proxies of a daemon's leases, each served on the daemon's
    event loop *loop* at ``service.web_socket_path(root, lease)``.

    The pool service runs in a worker thread, off the loop: ``open`` and
    ``close`` hand a proxy's start and stop to the loop and wait for them,
    and are not called on the loop itself. ``aclose`` runs on the loop when
    the daemon stops: it stops every proxy still served, and no proxy is
    served after it. *make* makes a lease's proxy; ``webproxy.WebProxy``
    unless given.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        root: Path | str,
        *,
        make: Callable[..., webproxy.WebProxy] = webproxy.WebProxy,
    ) -> None:
        self.loop = loop
        self.root = Path(root)
        self._make = make
        self._served: dict[str, webproxy.WebProxy] = {}
        # Stops handed to the loop from the loop's own thread, which cannot
        # wait for them; the daemon's stop waits for them instead.
        self._stopping: set[asyncio.Future] = set()
        self._closed = False
        self._lock = threading.Lock()

    def path(self, lease_id: str) -> Path:
        """Where the proxy of the lease *lease_id* is served."""
        return service.web_socket_path(self.root, lease_id)

    def sweep(self) -> None:
        """Remove every socket in the directory: one found there when the
        daemon starts was left by a daemon that did not stop cleanly, and no
        one serves it."""
        directory = self.root / service.WEB_DIRNAME
        if not directory.is_dir():
            return
        for child in sorted(directory.iterdir()):
            if stat.S_ISSOCK(child.lstat().st_mode):
                _log.warning("a web proxy socket no daemon serves, removed: %s", child)
                child.unlink(missing_ok=True)

    def open(self, lease_id: str, member: str, internal: Sequence[str]) -> Path:
        """Serve the proxy of the lease *lease_id*, on the member *member*,
        with the definition's internal hosts *internal*; the socket's path.

        Raises ``SpecfloError`` when the daemon is stopping, and what the
        proxy's start raises; nothing is served then.
        """
        if self._on_loop():
            raise RuntimeError("a web proxy is opened off the daemon's loop, which it waits on")
        path = self.path(lease_id)
        with self._lock:
            if self._closed:
                raise SpecfloError(
                    f"the daemon is stopping and serves no new web proxy, for lease {lease_id}."
                )
            if lease_id in self._served:
                raise SpecfloError(f"the web proxy of lease {lease_id} is served already.")
            proxy = self._make(path, internal, lease=lease_id, member=member, root=self.root)
            self._served[lease_id] = proxy
        try:
            path.parent.mkdir(mode=0o700, exist_ok=True)
            started = asyncio.run_coroutine_threadsafe(proxy.start(), self.loop)
            try:
                started.result(_WEB_SECONDS)
            except BaseException:
                # a start the loop has not finished is not left to finish later
                started.cancel()
                raise
        except BaseException:
            with self._lock:
                self._served.pop(lease_id, None)
            raise
        with self._lock:
            closed = self._closed
        if closed:
            # the daemon began to stop while this one started, and missed it
            self.close(lease_id, proxy)
            raise SpecfloError(
                f"the daemon is stopping and serves no new web proxy, for lease {lease_id}."
            )
        return path

    def close(self, lease_id: str, proxy: webproxy.WebProxy | None = None) -> None:
        """Stop the proxy of the lease *lease_id* and remove its socket; a
        lease with none served is left as it is. A stop that fails is logged,
        never raised: the lease has ended all the same."""
        with self._lock:
            served = self._served.pop(lease_id, None)
        proxy = proxy or served
        if proxy is None:
            return
        try:
            if self._on_loop():
                stopping = asyncio.ensure_future(proxy.stop())
                self._stopping.add(stopping)
                stopping.add_done_callback(self._stopping.discard)
                return
            asyncio.run_coroutine_threadsafe(proxy.stop(), self.loop).result(_WEB_SECONDS)
        except Exception as exc:
            _log.warning("the web proxy of lease %s did not stop: %s", lease_id, exc)
            # a loop that is gone serves nothing, and its socket is left behind
            with contextlib.suppress(OSError):
                if stat.S_ISSOCK(proxy.path.lstat().st_mode):
                    proxy.path.unlink()

    async def aclose(self) -> None:
        """Stop every proxy still served and remove its socket; no proxy is
        served after this. Run on the loop, when the daemon stops."""
        with self._lock:
            self._closed = True
            served, self._served = list(self._served.values()), {}
        await asyncio.gather(
            *(proxy.stop() for proxy in served), *self._stopping, return_exceptions=True
        )

    def _on_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False


class PoolInForce:
    """The pool service in force on *app* at each call, for the gateway's
    runs: a reload can put another in force. With none in force a grant is
    refused."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def grant(self, *args, **kwargs):
        return self._service().grant(*args, **kwargs)

    def end_lease(self, *args, **kwargs):
        return self._service().end_lease(*args, **kwargs)

    def _service(self):
        found = self.app.state.pool
        if found is None:
            raise SpecfloError("the daemon has no agent pool in force.")
        return found


@contextlib.asynccontextmanager
async def _serving(app: FastAPI) -> AsyncIterator[None]:
    """What runs while the daemon serves, and no longer.

    A daemon with a pool reads llama-swap's event stream for the model
    reloads of leased members, the event logs of leased hosted members for
    the calls their provider refused, and the key of every declared account
    for its figures. Each is a task on the server's event loop, cancelled
    when the server shuts down. The three run whatever the pool is, and each
    asks at every turn which pool is in force: a reload puts another
    configuration in force, and opens the pool of a daemon whose
    configuration did not stand at the start. A reader with nothing to read,
    and every reader of a daemon with no pool, reads nothing and waits.

    A root with a pool directory when the daemon starts has the bridge served
    on its socket until the daemon stops, whatever the configuration in the
    directory says. The filter asks the pool in force which models it holds,
    on each completion; with no pool in force it holds none. The socket is made before
    anything else runs, so a daemon that cannot have it - another daemon
    serves it - does not start.

    Such a root also has the web proxies of its web leases served on the
    daemon's loop, through ``WebProxies`` on ``app.state.web``, which
    the pool in force at the start is given. The sockets a daemon that died
    left in the directory are removed first, and when the daemon stops every
    proxy still served is stopped and its socket removed.

    A root whose gateway configuration stood at the start has the gateway
    loop run as one more task, over the poll step on
    ``app.state.gateway_poll``; the task is kept on ``app.state.gateway_task``.
    When the daemon stops, the gateway service on ``app.state.gateway_service``
    cancels its runs and waits for each to end. A root without one, or with a
    faulty one, has no such task and asks no forge.
    """

    def pool():
        return app.state.pool

    async with contextlib.AsyncExitStack() as serving:
        root = app.state.root
        if pool_dir(root).is_dir():
            upstream = os.environ.get(events.URL_ENV) or events.DEFAULT_URL
            await serving.enter_async_context(bridge.serving(
                bridge.socket_path(root), upstream,
                # the pool in force at each request: a reload can change it
                models=lambda: service.held_models(pool()),
            ))
            web = WebProxies(asyncio.get_running_loop(), root)
            web.sweep()
            serving.push_async_callback(web.aclose)
            app.state.web = web
            if pool() is not None:
                pool().web = web
        readers = (
            events.reader_for(pool()), watch.watcher_for(pool()), accounts.reader_for(pool())
        )
        readers = [reader for reader in readers if reader is not None]
        for reader in readers:
            # made for the pool of this moment, and told where the one in force is
            reader.pool = pool
        tasks = [asyncio.create_task(reader.run()) for reader in readers]
        if app.state.gateway is not None:
            loop = gateway.Gateway(app.state.gateway, app.state.gateway_poll)
            app.state.gateway_task = asyncio.create_task(loop.run(), name="gateway")
            tasks.append(app.state.gateway_task)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if app.state.gateway_service is not None:
                await app.state.gateway_service.aclose()


def create_app(root: Path, *, url: str | None = None) -> FastAPI:
    """The application serving the daemon root at ``root``.

    ``url`` is where the daemon answers, as its own agents must reach it;
    the seat a project's agent runs in is scaffolded with it. A root that
    still holds the tokens file from before named users has it moved into
    the state store first, before anything serves.

    The root's proxy sign-in setting is read once, onto
    ``app.state.proxy_auth``; with the proxy on, a setting that would trust
    the wrong peers raises before anything else is done.
    """
    root = prepare_root(root)
    proxy = proxy_auth(root)
    auth.migrate_legacy_tokens(root)
    # No generated docs or schema routes: those would serve every API path
    # to a browser without a token, and the token guard is the contract.
    app = FastAPI(
        title="specflo daemon", docs_url=None, redoc_url=None, openapi_url=None,
        lifespan=_serving,
    )
    app.state.root = root
    app.state.url = url
    app.state.proxy_auth = proxy
    # The chat pumps: one per agent discovery finds alive for a mapped
    # project, so a daemon restart follows every live conversation again.
    app.state.pumps = chat.Pumps(root)
    app.state.pumps.resume()
    # The agent pool the root's pool directory declares; None without one.
    # A directory with faults gives no pool either, and the faults are kept:
    # the projects are served, and the pool routes answer with the faults.
    app.state.pool, app.state.pool_errors = pool_routes.open_pool(root)
    # The issue gateway the root's gateway.yaml configures; None without one.
    # A file with faults gives none either, and the faults are logged and
    # kept: the daemon serves without the gateway.
    app.state.gateway, app.state.gateway_errors = open_gateway(root)
    # The service that runs research for the gateway's triggers, and its
    # poll step the gateway loop calls. Its runs lease from the pool in force.
    app.state.gateway_service = None
    app.state.gateway_poll = gateway.poll_nothing
    if app.state.gateway is not None:
        app.state.gateway_service = GatewayService(
            open_store(root), ServicePool(PoolInForce(app))
        )
        app.state.gateway_poll = app.state.gateway_service.poll
    app.state.gateway_task = None
    # The web proxies of the pool's web leases, made when the daemon serves.
    app.state.web = None

    @app.get(HEALTH_PATH)
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(router)
    app.include_router(pool_routes.router)
    web.install(app)
    return app
