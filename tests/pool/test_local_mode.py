"""Local mode: a checkout with no daemon has no pool, and pays nothing for one.

A pool is held by a daemon. In a checkout with no remote registered the three
``specflo lease`` verbs refuse, say that pools need a daemon, and do nothing
else: no process is started, no socket is opened and nothing is written. The
pool's own code and the web stack stay out of every local command, so the
import checks run in a fresh interpreter, where no other test has loaded them.
"""

import os
import socket
import subprocess
import sys
import textwrap

import pytest
from typer.testing import CliRunner

from specflo import config
from specflo.cli import app

runner = CliRunner()

LEASE_VERBS = (
    ["lease", "request", "workers"],
    ["lease", "release", "lease-4f2a9c0d1b7e3a55"],
    ["lease", "list"],
)

# What only a daemon loads: the pool's service side and the web stack.
DAEMON_ONLY = (
    "specflo.pool.service", "specflo.pool.runner", "specflo.pool.ledger",
    "specflo.pool.waiting", "specflo.daemon.app", "fastapi", "uvicorn", "starlette",
)


@pytest.fixture
def local_checkout(tmp_path, monkeypatch):
    """A checkout with no remote registered, and the working directory in it."""
    root = tmp_path / "checkout"
    root.mkdir()
    config.init_config(root)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def nothing_starts(monkeypatch):
    """Fail the test if anything starts a process or opens a socket."""

    def refuse(*args, **kwargs):
        pytest.fail("a lease verb in local mode started a process or opened a socket")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    for name in ("fork", "forkpty", "posix_spawn", "posix_spawnp", "system", "execv", "execve"):
        monkeypatch.setattr(os, name, refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _files(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _fresh(code, cwd=None):
    """Run *code* in a fresh interpreter; the finished process."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, cwd=cwd
    )


# --- the lease verbs refuse ----------------------------------------------


@pytest.mark.parametrize("verb", LEASE_VERBS, ids=lambda verb: verb[1])
def test_a_lease_verb_with_no_remote_says_pools_need_a_daemon(local_checkout, nothing_starts, verb):
    before = _files(local_checkout)

    result = runner.invoke(app, verb)

    assert result.exit_code == 1, result.output
    assert "Pools need a daemon" in result.stderr
    assert "specflo remote add" in result.stderr
    assert result.stdout == ""
    assert _files(local_checkout) == before


def test_the_refusal_comes_before_any_other_fault_in_the_request(local_checkout, nothing_starts):
    # With no daemon there is no pool to hold the limit against, so what is
    # wrong with the limit is not the news.
    result = runner.invoke(
        app, ["lease", "request", "workers", "--idle-limit", "soon", "--cwd", "missing"]
    )

    assert result.exit_code == 1
    assert "Pools need a daemon" in result.stderr
    assert "soon" not in result.stderr and "missing" not in result.stderr


def test_the_json_flag_changes_nothing_about_the_refusal(local_checkout, nothing_starts):
    result = runner.invoke(app, ["lease", "request", "workers", "--json"])

    assert result.exit_code == 1
    assert "Pools need a daemon" in result.stderr
    assert result.stdout == ""


# --- no pool code on a local path ----------------------------------------


def test_starting_the_cli_loads_no_pool_service_and_no_web_stack():
    done = _fresh(
        f"""
        import sys
        import specflo.cli
        print("loaded:", " ".join(n for n in {DAEMON_ONLY!r} if n in sys.modules))
        """
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "loaded:"


def test_a_local_command_loads_no_pool_service_and_no_web_stack(local_checkout):
    assert runner.invoke(app, ["new", "Thing"]).exit_code == 0

    done = _fresh(
        f"""
        import sys
        from specflo.cli import app
        try:
            app(["status"])
        except SystemExit as exit:
            assert exit.code in (0, None), exit.code
        print("pool:", " ".join(sorted(n for n in sys.modules if n.startswith("specflo.pool"))))
        print("loaded:", " ".join(n for n in {DAEMON_ONLY!r} if n in sys.modules))
        """,
        cwd=local_checkout,
    )

    assert done.returncode == 0, done.stderr
    assert "thing" in done.stdout
    # The pool group's declarations ride on the CLI; the code that reads a
    # pool configuration does not.
    assert "pool: specflo.pool specflo.pool.cli_admin\n" in done.stdout
    assert done.stdout.strip().endswith("loaded:")


def test_a_refused_lease_request_loads_no_pool_code_and_no_client(local_checkout):
    # The refusal is the checkout's own: not even the daemon client is loaded
    # to make it, and it is made on an install without the web stack.
    done = _fresh(
        f"""
        import sys
        for name in ("fastapi", "jinja2", "uvicorn", "starlette", "sse_starlette"):
            sys.modules[name] = None
        from specflo.cli import app
        try:
            app(["lease", "request", "workers"])
        except SystemExit as exit:
            print("exit:", exit.code)
        pool = sorted(n for n in sys.modules if n.startswith("specflo.pool"))
        print("pool:", " ".join(pool))
        print("client:", "specflo.service.pool_remote" in sys.modules)
        print("daemon:", " ".join(n for n in {DAEMON_ONLY!r} if sys.modules.get(n)))
        """,
        cwd=local_checkout,
    )

    assert done.returncode == 0, done.stderr
    assert "Pools need a daemon" in done.stderr
    lines = dict(line.split(":", 1) for line in done.stdout.strip().splitlines())
    assert lines["exit"].strip() == "1"
    assert set(lines["pool"].split()) == {
        "specflo.pool", "specflo.pool.cli_admin", "specflo.pool.cli_lease"
    }
    assert lines["client"].strip() == "False"
    assert lines["daemon"].strip() == ""
