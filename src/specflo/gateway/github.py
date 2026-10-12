"""Signing in to GitHub as the forge's GitHub App, with narrow tokens kept in memory.

A GitHub forge names its App's id and the file with the App's private key.
The key signs a short-lived JWT, and the JWT finds the App's installation on
a watched repo. An installation token then does the work on that repo, and
each token the gateway asks for is held to what the gateway needs: write on
issues, read on contents, and the one repo. The App may hold more on the
installation; a token never does.

Tokens, JWTs and the key live only in memory: githubkit keeps tokens in a
memory cache of this App's own, renews each before it runs out, and the
response cache is off, so nothing is written to a file. A fault names the
forge, the repo and the HTTP status, and never a secret; it does not chain
the githubkit fault, whose request still carries the Authorization header.

Every call is async (githubkit's ``async_*`` methods and ``arequest``), as
everything on the daemon's event loop is.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from githubkit import AppAuthStrategy, GitHub
from githubkit.cache import MemCacheStrategy
from githubkit.exception import GitHubException, RequestError, RequestFailed, RequestTimeout

from ..errors import SpecfloError
from .config import Forge, read_credential

#: What each installation token may do, and nothing more: the gateway reads
#: issues and posts on them, and reads the repo's files.
TOKEN_PERMISSIONS: dict[str, str] = {"issues": "write", "contents": "read"}


class GitHubApp:
    """The GitHub App of the forge *forge*, signed in with the key in its key file.

    The key is read once, here, through :func:`read_credential`, so a key file
    with the wrong mode or no key in it is refused before any call. *transport*
    stands in for the network in a test.
    """

    def __init__(self, forge: Forge, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if forge.app_id is None or forge.key_file is None:
            raise SpecfloError(f"forge '{forge.name}': not a GitHub App forge.")
        key = read_credential(forge.key_file)
        _check_key(forge, key)
        self.forge = forge
        self._app = GitHub(
            AppAuthStrategy(app_id=forge.app_id, private_key=key),
            base_url=forge.url,
            async_transport=transport,
            # Tokens stay in a memory cache of this App's own, never the
            # process-wide default, and no response is cached anywhere.
            cache_strategy=MemCacheStrategy(),
            http_cache=False,
        )
        self._installations: dict[str, int] = {}

    @asynccontextmanager
    async def session(self, repo: str) -> AsyncIterator[GitHub]:
        """A client on *repo* with an installation token held to issues and contents.

        The client's connections close when the block ends. A GitHub fault in
        the block, the token request included, comes out as a SpecfloError
        that names the forge, the repo and the status and holds no secret.
        """
        if repo not in self.forge.repos:
            raise SpecfloError(f"forge '{self.forge.name}': {repo} is not a watched repo.")
        try:
            installation_id = await self._installation_id(repo)
            client = self._app.with_auth(self._app.auth.as_installation(
                installation_id,
                repositories=[repo.split("/", 1)[1]],
                permissions=TOKEN_PERMISSIONS,
            ))
            async with client:
                yield client
        except (GitHubException, httpx.HTTPError) as exc:
            raise SpecfloError(f"forge '{self.forge.name}': {repo}: {_problem(exc)}") from None

    async def _installation_id(self, repo: str) -> int:
        """The id of the App's installation on *repo*, asked once and then kept."""
        if repo not in self._installations:
            owner, name = repo.split("/", 1)
            response = await self._app.rest.apps.async_get_repo_installation(owner, name)
            installation_id = response.json().get("id")
            if isinstance(installation_id, bool) or not isinstance(installation_id, int):
                raise SpecfloError(
                    f"forge '{self.forge.name}': {repo}: GitHub named no installation of the App."
                )
            self._installations[repo] = installation_id
        return self._installations[repo]


def _check_key(forge: Forge, key: str) -> None:
    """Refuse a key file that holds no RSA private key, without quoting it."""
    try:
        loaded = load_pem_private_key(key.encode("utf-8"), password=None)
    except (ValueError, TypeError):
        loaded = None
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise SpecfloError(f"{forge.key_file}: not a GitHub App's PEM private key.")


def _problem(exc: Exception) -> str:
    """What went wrong with a GitHub call, in words that hold no secret."""
    # A refused token request fails inside the auth flow, and githubkit
    # wraps that fault in another.
    while isinstance(exc, RequestError) and isinstance(exc.exc, GitHubException):
        exc = exc.exc
    if isinstance(exc, RequestFailed):
        return f"GitHub answered HTTP {exc.response.status_code}."
    if isinstance(exc, RequestTimeout):
        return "GitHub did not answer in time."
    if isinstance(exc, RequestError):
        return f"cannot reach GitHub ({type(exc.exc).__name__})."
    return f"the GitHub call failed ({type(exc).__name__})."
