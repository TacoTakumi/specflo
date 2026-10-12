"""Each run's scratch dir: where it lies, what it holds, and that it goes.

A run's scratch dir is made in the system temp dir, outside the daemon root,
and a member may work in it. It holds the run's input.md, a clone of the
issue's repo at its default branch and the wiki configuration that points the
agent at the operator's vault with the reader token. The clone is made with
the bot's credential, which ends up nowhere in the clone: its remote URL and
its git configuration hold no token. The dir is removed when the run ends,
whether it succeeded, failed, timed out or was cancelled, and a clone that
does not finish is stopped with its git.
"""

import asyncio
import base64
import os
import stat
import subprocess
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from leak_guard import scratch_prefix

from specflo import daemon
from specflo.gateway import clean, scratch
from specflo.pool import launch

TOKEN = "fake-forge-token-c41d"
WIKI_URL = "https://wiki.example:8443"
WIKI_TOKEN = "fake-wiki-reader-token-9e02"


#: The environment of every git these tests run: one that never asks anyone.
QUIET = scratch.git_environment(os.environ)


def _git(*args, cwd=None):
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, env=QUIET,
    )


@pytest.fixture
def origin(tmp_path):
    """A bare repo whose default branch is ``trunk``, beside a branch ``other``."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "trunk", cwd=work)
    (work / "README").write_text("on trunk\n", encoding="utf-8")
    _git("add", "README", cwd=work)
    _git("commit", "-q", "-m", "trunk", cwd=work)
    _git("checkout", "-q", "-b", "other", cwd=work)
    (work / "OTHER").write_text("on other\n", encoding="utf-8")
    _git("add", "OTHER", cwd=work)
    _git("commit", "-q", "-m", "other", cwd=work)
    bare = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(work), str(bare))
    _git("symbolic-ref", "HEAD", "refs/heads/trunk", cwd=bare)
    return bare


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


def _prepare(directory, url, **options):
    options.setdefault("wiki_url", WIKI_URL)
    options.setdefault("wiki_token", WIKI_TOKEN)
    options.setdefault("input_text", "# The issue\n")
    return scratch.prepare(directory, clone_url=url, **options)


def _branch(clone):
    return subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True, env=QUIET,
    ).stdout.strip()


def _remote_url(clone):
    return subprocess.run(
        ["git", "-C", str(clone), "config", "--get", "remote.origin.url"],
        check=True, capture_output=True, text=True, env=QUIET,
    ).stdout.strip()


def _holding(directory, secret):
    """The files under *directory* whose bytes hold *secret*."""
    found = []
    for path in Path(directory).rglob("*"):
        if path.is_file() and secret.encode() in path.read_bytes():
            found.append(path)
    return found


def test_a_prepared_scratch_dir_holds_the_input_the_clone_and_the_wiki_config(origin, root):
    text = clean.build_input("A question", "What does it do?", [(7, "Mind the edge.")], 7)
    url = origin.as_uri()

    async def run():
        async with scratch.scratch_dir() as directory:
            await _prepare(directory, url, input_text=text, token=TOKEN)
            seen = {
                "dir": directory,
                "input": (directory / scratch.INPUT_FILE).read_text(encoding="utf-8"),
                "branch": _branch(directory / scratch.CLONE_DIR),
                "files": sorted(p.name for p in (directory / scratch.CLONE_DIR).iterdir()),
                "remote": _remote_url(directory / scratch.CLONE_DIR),
                "holding": _holding(directory / scratch.CLONE_DIR, TOKEN),
                "wiki": yaml.safe_load(
                    (directory / scratch.WIKI_CONFIG).read_text(encoding="utf-8")
                ),
                "wiki_mode": stat.S_IMODE((directory / scratch.WIKI_CONFIG).stat().st_mode),
                "fault": launch.working_directory_fault(directory, os.environ, root),
                "environment": scratch.wiki_environment(directory),
            }
        return seen

    seen = asyncio.run(run())
    directory = seen["dir"]
    assert directory.parent == Path(tempfile.gettempdir())
    assert directory.name.startswith(scratch_prefix())
    assert os.path.commonpath([directory, root.resolve()]) != str(root.resolve())
    assert seen["fault"] is None
    assert seen["input"] == text
    assert seen["branch"] == "trunk"
    assert seen["files"] == [".git", "README"]
    assert seen["remote"] == url
    assert seen["holding"] == []
    assert seen["wiki"] == {
        "vaults": {"main": {"url": WIKI_URL, "token": WIKI_TOKEN}},
        "default_vault": "main",
    }
    assert seen["wiki_mode"] == 0o600
    assert seen["environment"] == {
        "AGENT_WIKI_CONFIG_DIR": str(directory / scratch.WIKI_CONFIG.parent)
    }
    assert not directory.exists()


def test_git_runs_in_an_environment_where_it_can_ask_nobody():
    base = {
        "PATH": "/usr/bin", "HOME": "/home/you",
        "SSH_ASKPASS": "/usr/bin/ksshaskpass", "GIT_ASKPASS": "/usr/bin/ksshaskpass",
        "SSH_ASKPASS_REQUIRE": "force", "GIT_TERMINAL_PROMPT": "1",
        "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.askPass",
        "GIT_CONFIG_VALUE_0": "/usr/bin/ksshaskpass",
    }
    environment = scratch.git_environment(base, token=TOKEN)
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["SSH_ASKPASS_REQUIRE"] == "never"
    for name in ("SSH_ASKPASS", "GIT_ASKPASS", "DISPLAY", "WAYLAND_DISPLAY"):
        assert name not in environment
    settings = {
        environment[f"GIT_CONFIG_KEY_{i}"]: environment[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(environment["GIT_CONFIG_COUNT"]))
    }
    assert settings["credential.helper"] == ""
    assert settings["core.askPass"] == ""
    assert settings["http.extraHeader"].startswith("Authorization: Basic ")
    assert environment["PATH"] == "/usr/bin" and environment["HOME"] == "/home/you"
    assert "http.extraHeader" not in scratch.git_environment(base).values()


def test_a_clone_runs_git_in_that_environment(origin, tmp_path):
    seen = tmp_path / "seen"
    program = tmp_path / "git"
    program.write_text(f"#!/bin/sh\nenv > {seen}\nexit 1\n", encoding="utf-8")
    program.chmod(0o755)
    clone = tmp_path / "clone"

    with pytest.raises(scratch.ScratchError):
        asyncio.run(scratch.clone(origin.as_uri(), clone, git=str(program)))
    lines = seen.read_text(encoding="utf-8").splitlines()
    assert "GIT_TERMINAL_PROMPT=0" in lines
    assert "SSH_ASKPASS_REQUIRE=never" in lines
    assert not [line for line in lines if line.startswith(("SSH_ASKPASS=", "GIT_ASKPASS="))]


class _Served:
    """A bare repo served over git's dumb http protocol to a client that
    brings the token in a basic Authorization header, and to nobody else."""

    def __init__(self, bare, username, token):
        _git("update-server-info", cwd=bare)
        expected = "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
        self.headers = []
        headers = self.headers

        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                headers.append(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != expected:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="repo"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                super().do_GET()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(Handler, directory=str(bare.parent))
        )
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/{bare.name}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def test_the_credential_reaches_the_forge_and_stays_out_of_the_clone(origin):
    async def run():
        async with scratch.scratch_dir() as directory:
            await _prepare(directory, served.url, token=TOKEN, depth=None)
            clone = directory / scratch.CLONE_DIR
            return _branch(clone), _remote_url(clone), _holding(directory, TOKEN)

    with _Served(origin, scratch.TOKEN_USERNAME, TOKEN) as served:
        branch, remote, holding = asyncio.run(run())
    assert branch == "trunk"
    assert remote == served.url
    assert holding == []
    assert served.headers and all(header is not None for header in served.headers)


def test_a_clone_without_the_credential_is_refused_and_the_dir_still_goes(origin):
    made = []

    async def run():
        async with scratch.scratch_dir() as directory:
            made.append(directory)
            await _prepare(directory, served.url, depth=None)

    with _Served(origin, scratch.TOKEN_USERNAME, TOKEN) as served:
        with pytest.raises(scratch.ScratchError, match="clone"):
            asyncio.run(run())
    assert not made[0].exists()


def test_a_clone_url_that_carries_a_credential_is_refused(origin):
    async def run():
        async with scratch.scratch_dir() as directory:
            await _prepare(directory, f"https://bot:{TOKEN}@forge.example/me/thing.git")

    with pytest.raises(scratch.ScratchError) as caught:
        asyncio.run(run())
    assert TOKEN not in str(caught.value)


def test_the_dir_is_removed_when_the_run_fails(origin):
    made = []

    async def run():
        async with scratch.scratch_dir() as directory:
            made.append(directory)
            await _prepare(directory, origin.as_uri())
            raise RuntimeError("the agent failed")

    with pytest.raises(RuntimeError, match="the agent failed"):
        asyncio.run(run())
    assert made and not made[0].exists()


def test_the_dir_is_removed_when_the_run_times_out(origin):
    made = []

    async def run():
        async with asyncio.timeout(0.5):
            async with scratch.scratch_dir() as directory:
                made.append(directory)
                await _prepare(directory, origin.as_uri())
                await asyncio.sleep(60)

    with pytest.raises(TimeoutError):
        asyncio.run(run())
    assert made and not made[0].exists()


def test_the_dir_is_removed_when_the_run_is_cancelled(origin):
    made = []

    async def run():
        ready = asyncio.Event()

        async def body():
            async with scratch.scratch_dir() as directory:
                made.append(directory)
                await _prepare(directory, origin.as_uri())
                ready.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(body())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert made and not made[0].exists()


def test_the_dir_is_removed_when_the_clone_fails(tmp_path):
    made = []

    async def run():
        async with scratch.scratch_dir() as directory:
            made.append(directory)
            await _prepare(directory, (tmp_path / "missing.git").as_uri())

    with pytest.raises(scratch.ScratchError, match="clone"):
        asyncio.run(run())
    assert made and not made[0].exists()


@pytest.fixture
def stuck_git(tmp_path):
    """A git that starts a child of its own and never finishes."""
    program = tmp_path / "git"
    program.write_text("#!/bin/sh\nsleep 60 &\nexec sleep 60\n", encoding="utf-8")
    program.chmod(0o755)
    return str(program)


def test_a_clone_that_does_not_finish_is_stopped_with_its_git(origin, stuck_git):
    made = []

    async def run():
        async with scratch.scratch_dir() as directory:
            made.append(directory)
            await _prepare(directory, origin.as_uri(), git=stuck_git, timeout=0.5)

    with pytest.raises(scratch.ScratchError, match="did not finish"):
        asyncio.run(run())
    assert made and not made[0].exists()


def test_a_cancelled_clone_is_stopped_with_its_git(origin, stuck_git):
    made = []

    async def run():
        async def body():
            async with scratch.scratch_dir() as directory:
                made.append(directory)
                await _prepare(directory, origin.as_uri(), git=stuck_git)

        task = asyncio.create_task(body())
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert made and not made[0].exists()
