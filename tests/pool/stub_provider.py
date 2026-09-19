"""A fake provider for the pool's account reads: no socket, no network.

It answers the way the provider's key-read endpoint does, keyed by the API
key a request carries, and it remembers every request it was sent so a test
can show that nothing but that endpoint was ever called. It is an httpx
transport, so the reader under test runs its real request code against it.
"""

from __future__ import annotations

import dataclasses

import httpx

URL = "https://provider.invalid"
KEY_READ_PATH = "/api/v1/key"


@dataclasses.dataclass(frozen=True)
class Seen:
    """One request the stub was sent."""

    method: str
    path: str
    authorization: str | None


class StubProvider:
    """The provider, holding what each key's read returns.

    ``unreachable`` makes every request fail to connect; ``status`` other than
    200 makes every read answer that status; ``body`` replaces the answer's
    text, for a provider that answers something that is not the key record.
    """

    def __init__(self) -> None:
        self.keys: dict[str, dict] = {}
        self.seen: list[Seen] = []
        self.unreachable = False
        self.status = 200
        self.body: str | None = None

    def set_key(
        self,
        key: str,
        *,
        usage: float = 0.0,
        limit: float | None = None,
        limit_remaining: float | None = None,
        free_limit: int = 50,
        free_used: int = 0,
    ) -> None:
        """Declare ``key`` and what a read of it reports."""
        self.keys[key] = {
            "label": "sk-or-v1-abc...123",
            "limit": limit,
            "limit_reset": None,
            "limit_remaining": limit_remaining,
            "include_byok_in_limit": False,
            "usage": usage,
            "usage_daily": usage,
            "usage_weekly": usage,
            "usage_monthly": usage,
            "is_free_tier": True,
            "free_model_daily_requests": {
                "used": free_used,
                "limit": free_limit,
                "remaining": max(free_limit - free_used, 0),
            },
        }

    def client(self) -> httpx.Client:
        """A client whose every request lands here."""
        return httpx.Client(transport=httpx.MockTransport(self._answer), base_url=URL)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("authorization")
        self.seen.append(Seen(request.method, request.url.path, authorization))
        if self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        if request.method != "GET" or request.url.path != KEY_READ_PATH:
            return httpx.Response(404, json={"error": {"code": 404, "message": "Not found"}})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"code": self.status}})
        if self.body is not None:
            return httpx.Response(200, text=self.body)
        key = (authorization or "").removeprefix("Bearer ")
        if key not in self.keys:
            return httpx.Response(401, json={"error": {"code": 401, "message": "No auth"}})
        return httpx.Response(200, json={"data": self.keys[key]})
