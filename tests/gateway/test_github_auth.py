"""The gateway signs in to GitHub as a GitHub App, with narrow tokens it keeps in memory.

The App's private key signs a JWT, and the JWT finds the App's installation on
a watched repo. Each installation token the gateway asks for names only the
permissions it needs, write on issues and read on contents, for the one repo
it serves. The token stays in the client's memory: it is never written to a
file, a log or a fault. All of it runs through githubkit's async client.

The GitHub here is a stub on an httpx mock transport, and the App's key is a
throwaway RSA key made for the test.
"""

import ast
import asyncio
import json
import logging
import os
import secrets
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from specflo.errors import SpecfloError
from specflo.gateway import github
from specflo.gateway.config import GITHUB, GITHUB_API, Forge

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ID = 99
INSTALLATION_ID = 4711


@pytest.fixture(scope="module")
def app_key():
    """A throwaway RSA key: the private half in PEM, and the public half."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return pem, key.public_key()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Every place a library might write to by default lies under tmp_path."""
    for name in ("HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "TMPDIR"):
        place = tmp_path / name.lower()
        place.mkdir()
        monkeypatch.setenv(name, str(place))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def forge(home, app_key):
    key_file = home / "daemon" / "github-app.pem"
    key_file.parent.mkdir()
    key_file.write_text(app_key[0], encoding="ascii")
    os.chmod(key_file, 0o600)
    return Forge(
        name="github", kind=GITHUB, url=GITHUB_API, repos=("me/thing", "me/other"),
        operator_id=4242, pool="rfc-research", app_id=APP_ID, key_file=key_file,
    )


class StubGitHub:
    """GitHub's App endpoints and one issue list, answering on a mock transport.

    It checks the App's JWT against the public key, hands out a fresh token
    for each installation token request, and records each request body.
    """

    def __init__(self, public_key, *, refuse_tokens=False):
        self.public_key = public_key
        self.refuse_tokens = refuse_tokens
        self.token_bodies: list[dict] = []
        self.tokens: list[str] = []
        self.jwts: list[str] = []
        self.issue_reads: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        authorization = request.headers.get("Authorization", "")
        if path.endswith("/installation") and request.method == "GET":
            if not self._app_signed(authorization):
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(200, json={"id": INSTALLATION_ID})
        if path == f"/app/installations/{INSTALLATION_ID}/access_tokens":
            if not self._app_signed(authorization) or self.refuse_tokens:
                return httpx.Response(401, json={"message": "Bad credentials"})
            self.token_bodies.append(_json(request))
            token = "ghs_" + secrets.token_hex(16)
            self.tokens.append(token)
            expires = datetime.now(timezone.utc) + timedelta(hours=1)
            return httpx.Response(201, json={
                "token": token, "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        if path.endswith("/issues") and request.method == "GET":
            scheme, _, token = authorization.partition(" ")
            if scheme != "token" or token not in self.tokens:
                return httpx.Response(401, json={"message": "Bad credentials"})
            self.issue_reads.append(path)
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "Not Found"})

    def _app_signed(self, authorization: str) -> bool:
        scheme, _, token = authorization.partition(" ")
        if scheme != "Bearer":
            return False
        try:
            claims = jwt.decode(token, self.public_key, algorithms=["RS256"])
        except jwt.PyJWTError:
            return False
        self.jwts.append(token)
        return claims.get("iss") == str(APP_ID)


def _json(request: httpx.Request) -> dict:
    return json.loads(request.content)


def _run(coroutine):
    return asyncio.run(coroutine)


def _files_holding(root: Path, secret: str) -> list[Path]:
    found = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            if secret.encode() in path.read_bytes():
                found.append(path)
    return found


def test_githubkit_rides_with_the_serve_extra():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    serve = project["project"]["optional-dependencies"]["serve"]
    named = [spec for spec in serve if spec.startswith("githubkit")]
    assert len(named) == 1
    assert "[auth-app]" in named[0] and ">=" in named[0]


def test_each_token_request_names_only_issues_write_and_contents_read(forge, app_key):
    stub = StubGitHub(app_key[1])

    async def scenario():
        app = github.GitHubApp(forge, transport=stub.transport())
        for repo in ("me/thing", "me/other", "me/thing"):
            async with app.session(repo) as client:
                await client.arequest("GET", f"/repos/{repo}/issues")

    _run(scenario())

    assert stub.issue_reads == [
        "/repos/me/thing/issues", "/repos/me/other/issues", "/repos/me/thing/issues",
    ]
    # One token per repo: the second read of me/thing reuses the one in memory.
    assert stub.token_bodies == [
        {"permissions": {"issues": "write", "contents": "read"}, "repositories": ["thing"]},
        {"permissions": {"issues": "write", "contents": "read"}, "repositories": ["other"]},
    ]


def test_no_token_or_key_is_on_disk_or_in_the_log_after_a_run(forge, app_key, home, caplog):
    stub = StubGitHub(app_key[1])

    async def scenario():
        app = github.GitHubApp(forge, transport=stub.transport())
        async with app.session("me/thing") as client:
            await client.arequest("GET", "/repos/me/thing/issues")

    with caplog.at_level(logging.DEBUG):
        _run(scenario())

    assert stub.tokens and stub.jwts
    secrets_seen = stub.tokens + stub.jwts
    for secret in secrets_seen:
        assert _files_holding(home, secret) == []
        assert secret not in caplog.text
    key_body = app_key[0].splitlines()[1]
    assert key_body not in caplog.text
    # The key's own file is the one place the key lies.
    assert _files_holding(home, key_body) == [forge.key_file]


def test_a_refused_token_raises_a_fault_without_secrets(forge, app_key, caplog):
    stub = StubGitHub(app_key[1], refuse_tokens=True)

    async def scenario():
        app = github.GitHubApp(forge, transport=stub.transport())
        async with app.session("me/thing") as client:
            await client.arequest("GET", "/repos/me/thing/issues")

    with caplog.at_level(logging.DEBUG), pytest.raises(SpecfloError) as raised:
        _run(scenario())

    message = str(raised.value)
    assert "me/thing" in message and "401" in message
    for secret in stub.jwts:
        assert secret not in message
        assert secret not in caplog.text
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_a_repo_the_forge_does_not_watch_gets_no_token(forge, app_key):
    stub = StubGitHub(app_key[1])

    async def scenario():
        app = github.GitHubApp(forge, transport=stub.transport())
        async with app.session("someone/else"):
            pass

    with pytest.raises(SpecfloError, match="someone/else"):
        _run(scenario())
    assert stub.token_bodies == [] and stub.jwts == []


def test_a_key_file_opened_up_is_refused(forge, app_key):
    os.chmod(forge.key_file, 0o644)
    with pytest.raises(SpecfloError, match="0600"):
        github.GitHubApp(forge, transport=StubGitHub(app_key[1]).transport())


def test_a_key_file_with_no_key_is_refused_without_quoting_it(forge, app_key):
    forge.key_file.write_text("not-a-key-5e1d\n", encoding="ascii")
    with pytest.raises(SpecfloError, match="private key") as raised:
        github.GitHubApp(forge, transport=StubGitHub(app_key[1]).transport())
    assert "not-a-key-5e1d" not in str(raised.value)


def test_the_github_module_makes_no_synchronous_githubkit_call():
    """Every githubkit call goes through its async methods, so the daemon's loop never blocks."""
    source = (REPO_ROOT / "src" / "specflo" / "gateway" / "github.py").read_text(encoding="utf-8")
    sync_calls = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            under_rest = "rest" in ast.unparse(node.func.value).split(".")
            if (under_rest and not name.startswith("async_")) or name in ("request", "graphql"):
                sync_calls.append(ast.unparse(node.func))
    assert sync_calls == []
