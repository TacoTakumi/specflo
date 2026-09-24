"""Shared pytest fixtures and helpers for the specflo test suite."""

import ast
import inspect
import textwrap
from pathlib import Path

import pytest


def executable_identifiers(obj) -> str:
    """Every identifier and string *value* in ``obj``, docstrings excluded.

    Source-scan tests assert that a module or function does not *do* something —
    read auto-run state, compose its own continuation wording, name a harness
    trigger. A raw substring scan over the source cannot express that: it also
    flags comments and docstrings describing the very thing being ruled out, so
    documenting an invariant would break its own test.

    This walks the AST instead. Comments never reach it, statement-position
    docstrings are dropped, and what remains is what the code actually evaluates:
    names, attributes, arguments, imports, and string literals (f-string parts
    included). Accepts a module or a function.
    """
    # dedent, never cleandoc: cleandoc strips the common indent of lines 2+ while
    # leaving line 1 alone, which de-indents a function's body relative to its own
    # `def` and raises IndentationError. A decorated function happens to survive
    # that (its line 2 is the `def` at column 0), which is exactly the kind of
    # accident this helper should not rely on.
    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
    scopes = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, scopes) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body.pop(0)

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            found.append(node.attr)
        elif isinstance(node, ast.arg):
            found.append(node.arg)
        elif isinstance(node, ast.alias):
            found.append(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
    return "\n".join(found).lower()


def live_keys(text: str) -> list[str]:
    """The keys actually set in a config file - commented-out entries excluded.

    The file carries every registry key, so "is autonomy in the file" no longer
    answers "is autonomy set"; asking for the live keys does.
    """
    return [
        line.split(":", 1)[0]
        for line in text.splitlines()
        if ":" in line and not line.startswith("#")
    ]
@pytest.fixture(autouse=True)
def _no_directory_override(monkeypatch):
    """Keep ``SPECFLO_DIRECTORY`` out of the suite's environment.

    The variable redirects every specflo command (root-option REQ-02), so a
    caller that exported it would otherwise steer the whole suite away from
    each test's tmp repo. Tests that exercise the env var set it themselves.
    """
    monkeypatch.delenv("SPECFLO_DIRECTORY", raising=False)


@pytest.fixture(autouse=True)
def _no_checkout_remotes(monkeypatch, tmp_path_factory):
    """Make the remote lookup find nothing in this checkout.

    The checkout may hold a registered remote with a durable token for a live
    daemon, and a test that runs a verb from the repository root would reach
    it. Every lookup of the checkout's remotes lands in an empty directory
    instead, so a verb that picks a remote fails closed. A test's own root
    keeps its real remotes directory.
    """
    from specflo import config

    checkout = Path(__file__).resolve().parents[1]
    real = config.remotes_dir

    def remotes_dir(root: Path) -> Path:
        if Path(root).resolve() == checkout:
            return tmp_path_factory.mktemp("no-remotes")
        return real(root)

    monkeypatch.setattr(config, "remotes_dir", remotes_dir)


@pytest.fixture(autouse=True)
def _fresh_config_warnings():
    """Clear the once-per-process invalid-value warnings between tests.

    `load_config` warns at most once per bad key per process (REQ-26). The whole
    suite is one process, so without this a test that expects a warning would
    pass or fail depending on which test ran before it.
    """
    from specflo import config

    config.reset_warnings()
    yield
    config.reset_warnings()


@pytest.fixture
def fixture_roots(tmp_path, monkeypatch):
    """Redirect agentsquire's harness roots to throwaway fixture dirs (D-12).

    agentsquire reads AGENTSQUIRE_HOME / AGENTSQUIRE_PROJECT to locate the
    user-scope and project-scope roots; each carries a `.claude/` marker so the
    claude-code harness is detected. This keeps `specflo skills` verbs off the
    real ~/.claude during tests -- the live-edit dev symlinks are never touched.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".claude").mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    monkeypatch.setenv("AGENTSQUIRE_HOME", str(home))
    monkeypatch.setenv("AGENTSQUIRE_PROJECT", str(project))
    return home, project


@pytest.fixture
def live_daemon(tmp_path):
    """A real specflo daemon on a free loopback port, with a root of its own.

    Yields the daemon's root, its URL, and a developer token. Requests reach
    it over a real socket, so what a test sees is what a client sees.
    """
    import threading
    import time

    import uvicorn

    from specflo import daemon
    from specflo.daemon import auth
    from specflo.daemon.app import create_app

    root = daemon.prepare_root(tmp_path / "daemon")
    server = uvicorn.Server(
        uvicorn.Config(create_app(root), host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "the daemon did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield {
        "root": root,
        "url": f"http://127.0.0.1:{port}",
        "token": auth.mint_token(root, "developer"),
    }
    server.should_exit = True
    thread.join(timeout=10)
