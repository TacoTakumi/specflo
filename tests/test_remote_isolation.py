"""No test reaches a remote registered in the real checkout.

Remotes live under ``<checkout>/.specflo/remotes``, found from the directory a
command starts in. This checkout can hold a registration with a durable token
for a live daemon, so a test that runs a verb from the repository root would
send a real request to it. An autouse fixture in the top-level conftest makes
the lookup find nothing there; a verb that picks a remote then fails closed.
"""

import ast
import socket
from pathlib import Path

from typer.testing import CliRunner

from specflo import config
from specflo.cli import app

REPO = Path(__file__).resolve().parents[1]
FIXTURE = "_no_checkout_remotes"


def test_the_real_checkout_has_no_registered_remote_under_the_suite():
    assert config.list_remotes(REPO) == {}


def test_lease_request_from_the_checkout_finds_no_remote_and_opens_no_connection(
    monkeypatch,
):
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise OSError("a test tried to open a connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.chdir(REPO)

    result = CliRunner().invoke(app, ["lease", "request", "workers", "--wait", "0"])

    assert result.exit_code != 0
    assert "has no remote" in result.output
    assert attempts == []


def test_the_isolating_fixture_applies_to_this_test(request):
    assert FIXTURE in request.fixturenames


def test_the_isolating_fixture_is_autouse_in_the_top_level_conftest():
    tree = ast.parse((REPO / "tests" / "conftest.py").read_text(encoding="utf-8"))
    (definition,) = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == FIXTURE
    ]
    autouse = [
        keyword.value.value
        for decorator in definition.decorator_list
        if isinstance(decorator, ast.Call)
        for keyword in decorator.keywords
        if keyword.arg == "autouse" and isinstance(keyword.value, ast.Constant)
    ]
    assert autouse == [True]
