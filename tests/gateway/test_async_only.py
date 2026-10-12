"""No module of the gateway makes a synchronous network call.

The gateway runs on the daemon's event loop, so a call that blocks on the
network stalls the daemon and every other forge with it. Every forge call is
async: Gitea through httpx's AsyncClient, GitHub through githubkit's async
client. This test reads the source of every module in the gateway package,
those written later too, and finds none of the synchronous clients: requests,
urllib.request, http.client, urllib3, httpx's Client and its module-level
calls, and the blocking calls of the socket module.
"""

import ast
from pathlib import Path

import pytest

import specflo.gateway

PACKAGE = Path(specflo.gateway.__file__).parent

#: Modules that hold only synchronous clients: importing one is a fault.
SYNC_MODULES = ("requests", "urllib.request", "http.client", "urllib3", "httplib2", "pycurl")

#: Names that are a synchronous client or call, by the module they come from.
SYNC_NAMES = {
    "httpx": {
        "Client", "HTTPTransport", "request", "stream",
        "get", "post", "put", "patch", "delete", "head", "options",
    },
    "socket": {
        "socket", "create_connection", "create_server", "socketpair", "fromfd",
        "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo",
    },
    "urllib": {"request"},
    "http": {"client"},
}


def sync_calls(source: str, filename: str = "<source>") -> list[str]:
    """Each synchronous network client that *source* imports or names, with its line."""
    tree = ast.parse(source, filename)
    # What each local name stands for: a module, or a name from a module.
    modules: dict[str, str] = {}
    found: list[str] = []

    def fault(node: ast.AST, what: str) -> None:
        found.append(f"{filename}:{node.lineno}: {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _sync_module(alias.name):
                    fault(node, f"import {alias.name}")
                modules[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if _sync_module(node.module):
                fault(node, f"from {node.module} import ...")
                continue
            for alias in node.names:
                named = alias.name in SYNC_NAMES.get(node.module, ())
                if named or _sync_module(f"{node.module}.{alias.name}"):
                    fault(node, f"from {node.module} import {alias.name}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module = modules.get(node.value.id)
            if module is not None and node.attr in SYNC_NAMES.get(module, ()):
                fault(node, f"{node.value.id}.{node.attr}")
    return found


def _sync_module(name: str) -> bool:
    return any(name == module or name.startswith(module + ".") for module in SYNC_MODULES)


def _modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_the_scan_covers_every_module_of_the_gateway():
    names = {path.name for path in _modules()}
    assert {"__init__.py", "config.py", "clean.py", "forge.py", "gitea.py"} <= names


def test_no_gateway_module_makes_a_synchronous_network_call():
    found = []
    for path in _modules():
        found += sync_calls(path.read_text(encoding="utf-8"), str(path.relative_to(PACKAGE)))
    assert found == []


def test_the_gitea_client_is_httpx_async():
    source = (PACKAGE / "gitea.py").read_text(encoding="utf-8")
    assert "httpx.AsyncClient" in source


@pytest.mark.parametrize("source", [
    "import requests",
    "import requests as r",
    "from requests import Session",
    "import urllib.request",
    "from urllib import request",
    "from urllib.request import urlopen",
    "import http.client",
    "from http import client",
    "from http.client import HTTPSConnection",
    "import urllib3",
    "import httpx\nhttpx.Client()",
    "import httpx as h\nh.get('https://x')",
    "import httpx\nhttpx.post('https://x')",
    "import httpx\nwith httpx.stream('GET', 'https://x'): pass",
    "from httpx import Client",
    "from httpx import get",
    "import httpx\nhttpx.HTTPTransport()",
    "import socket\nsocket.create_connection(('x', 1))",
    "import socket\nsocket.socket()",
    "import socket\nsocket.getaddrinfo('x', 1)",
    "from socket import create_connection",
])
def test_the_scan_finds_each_kind_of_synchronous_call(source):
    assert sync_calls(source) != []


@pytest.mark.parametrize("source", [
    "import httpx\nhttpx.AsyncClient()",
    "import httpx\nhttpx.AsyncHTTPTransport()",
    "from httpx import AsyncClient, Response, HTTPStatusError",
    "import socket\nsocket.AF_INET",
    "import asyncio\nasyncio.open_connection('x', 1)",
    "import urllib.parse\nurllib.parse.urlsplit('https://x')",
    "from urllib.parse import urlsplit",
])
def test_the_scan_lets_async_calls_and_plain_helpers_be(source):
    assert sync_calls(source) == []
