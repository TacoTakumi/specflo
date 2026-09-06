"""The remote ProjectService: every operation is an HTTP call to a daemon.

Holds a daemon URL and the bearer token a client presents, and sends each
operation to the daemon's route for it with the arguments as JSON. The
result comes back through the wire layer as the same values the local
service returns in-process, and a refusal comes back as the same
``SpecfloError`` the local service would have raised, so a command cannot
tell which of the two it is talking to.

The methods are generated from the protocol: each carries the protocol's
own signature, binds its arguments the way a direct call would, and posts
them. Adding an operation to the protocol adds it here, exactly as it adds
its route to the daemon.
"""

from __future__ import annotations

import functools
import inspect
import json

import httpx

from ..errors import SpecfloError
from . import wire
from .protocol import ProjectService


class RemoteProjectService:
    """A ``ProjectService`` over HTTP to the daemon at ``url``.

    ``client`` lets a caller supply the HTTP client (a test drives the daemon
    in-process through one); otherwise one is opened against ``url``.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.client = (
            client if client is not None else httpx.Client(base_url=self.url, timeout=timeout)
        )
        self.client.headers["Authorization"] = f"Bearer {token}"

    def _call(self, operation: str, arguments: dict):
        try:
            response = self.client.post(wire.route_path(operation), json=wire.encode(arguments))
        except httpx.HTTPError as exc:
            raise SpecfloError(f"Cannot reach the remote at {self.url}: {exc}") from exc
        if response.status_code == 200:
            return wire.decode(response.json()["result"], wire.OPERATIONS[operation].returns)
        detail = response_detail(response)
        if response.status_code == 401:
            raise SpecfloError(f"The remote at {self.url} refused the token: {detail}")
        if response.status_code in (400, 422):
            raise SpecfloError(detail)
        raise SpecfloError(
            f"The remote at {self.url} answered {response.status_code} to {operation}: {detail}"
        )


def response_detail(response: httpx.Response) -> str:
    """The message a daemon response carries, whatever shape it came in."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()
    detail = body.get("detail") if isinstance(body, dict) else body
    return detail if isinstance(detail, str) else json.dumps(detail)


def _operation(operation: wire.Operation):
    protocol_method = getattr(ProjectService, operation.name)
    signature = inspect.signature(protocol_method)

    @functools.wraps(protocol_method)
    def method(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        arguments = dict(bound.arguments)
        arguments.pop("self")
        return self._call(operation.name, arguments)

    return method


for _spec in wire.OPERATIONS.values():
    setattr(RemoteProjectService, _spec.name, _operation(_spec))
