"""Each run's scratch dir: the one place the research agent works in.

A run gets a dir of its own in the system temp dir, made by
:func:`scratch_dir` and removed when the run ends, whether it succeeded,
failed, timed out or was cancelled. It lies outside the daemon root, so a
member may work in it (the sandbox mounts a fresh ``/tmp`` and binds the dir
back on top). Its name starts with ``specflo-run-<pid>-``, the daemon's pid,
so a dir left by a daemon that died can be told from the dirs of one that
runs.

:func:`prepare` fills it with three things:

- ``input.md``, the cleaned issue text (:func:`clean.build_input`);
- ``repo``, a clone of the issue's repo at its default branch;
- ``.agent-wiki/config.yaml``, which points ``awiki`` at the operator's vault
  on its server with a token that may only read.

The clone is made with the bot's forge credential, and the agent must never
hold that. So the credential goes to git as configuration in git's
environment, a basic Authorization header that git sends with each request
and does not write anywhere, never in the URL or on the command line. A URL
that carries a credential of its own is refused, and after the clone the
remote URL and the clone's git configuration are checked to hold none.

git runs as a child of the event loop, in a process group of its own. A clone
that does not finish in time, or whose run is cancelled, is stopped with
every process of its group and reaped before the dir goes.

``awiki`` reads a project's ``.agent-wiki/config.yaml`` only under the home
and only once the operator has trusted it, and the scratch dir is neither.
The file is written so that it also serves as the user configuration, and
:func:`wiki_environment` is what the member's environment needs to be pointed
at it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import shutil
import signal
import stat
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..errors import SpecfloError

#: The run's input, the cleaned issue text.
INPUT_FILE = "input.md"

#: The clone of the issue's repo.
CLONE_DIR = "repo"

#: The wiki configuration, relative to the scratch dir.
WIKI_CONFIG = Path(".agent-wiki") / "config.yaml"

#: The name the wiki configuration gives the operator's vault.
WIKI_VAULT = "main"

#: The variable that tells ``awiki`` where its user configuration lies.
WIKI_CONFIG_VAR = "AGENT_WIKI_CONFIG_DIR"

#: The user name sent with the forge token. GitHub wants this one for an App
#: installation token, and Gitea takes the token whatever name comes with it.
TOKEN_USERNAME = "x-access-token"

#: Seconds a clone may take before it is stopped.
CLONE_TIMEOUT = 300.0

#: The variables through which git could ask a person: the askpass programs,
#: and the desktop a dialog would open on.
_ASKING = ("GIT_ASKPASS", "SSH_ASKPASS", "DISPLAY", "WAYLAND_DISPLAY")

#: How much of git's own complaint a fault quotes.
_GIT_ERROR_LIMIT = 500

_log = logging.getLogger(__name__)


class ScratchError(SpecfloError):
    """A scratch dir that could not be prepared. The message names no credential."""


def scratch_prefix() -> str:
    """The name prefix of the scratch dirs this process makes."""
    return f"specflo-run-{os.getpid()}-"


@contextlib.asynccontextmanager
async def scratch_dir() -> AsyncIterator[Path]:
    """A new scratch dir in the system temp dir, removed when the block ends in any way."""
    directory = Path(tempfile.mkdtemp(prefix=scratch_prefix()))
    try:
        yield directory
    finally:
        _remove(directory)


async def prepare(
    directory: Path,
    *,
    input_text: str,
    clone_url: str,
    wiki_url: str,
    wiki_token: str,
    token: str | None = None,
    username: str = TOKEN_USERNAME,
    depth: int | None = 1,
    timeout: float = CLONE_TIMEOUT,
    git: str = "git",
) -> None:
    """Fill the scratch dir *directory* for a run.

    *input_text* is written to ``input.md``. *clone_url* is cloned at its
    default branch, *depth* commits deep (the whole history for None), with
    *token* sent as the password of *username* when there is one. The wiki
    configuration names *wiki_url* and the reader token *wiki_token*.
    """
    directory = Path(directory)
    (directory / INPUT_FILE).write_text(input_text, encoding="utf-8")
    write_wiki_config(directory, wiki_url, wiki_token)
    await clone(
        clone_url, directory / CLONE_DIR, token=token, username=username,
        depth=depth, timeout=timeout, git=git,
    )


def write_wiki_config(directory: Path, url: str, token: str) -> Path:
    """Write the wiki configuration naming the vault at *url* and the reader *token*."""
    path = Path(directory) / WIKI_CONFIG
    path.parent.mkdir(mode=0o700, exist_ok=True)
    data = {"vaults": {WIKI_VAULT: {"url": url, "token": token}}, "default_vault": WIKI_VAULT}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)
    return path


def wiki_environment(directory: Path) -> dict[str, str]:
    """The environment that points the member's ``awiki`` at the scratch dir's configuration."""
    return {WIKI_CONFIG_VAR: str(Path(directory) / WIKI_CONFIG.parent)}


async def clone(
    url: str,
    destination: Path,
    *,
    token: str | None = None,
    username: str = TOKEN_USERNAME,
    depth: int | None = 1,
    timeout: float = CLONE_TIMEOUT,
    git: str = "git",
) -> None:
    """Clone *url* at its default branch into *destination*, which must not exist yet.

    The clone's remote is *url*, which must carry no credential; *token*
    reaches the forge only through git's environment. A clone that fails or
    does not finish in *timeout* seconds raises :class:`ScratchError`.
    """
    shown = _shown(url)
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ScratchError(f"the clone URL {shown} carries a credential; give it without one.")
    argv = [git, "clone", "--quiet", "--no-tags"]
    if depth is not None:
        argv += ["--depth", str(depth)]
    argv += ["--", url, str(destination)]
    environment = git_environment(os.environ, token=token, username=username)
    returncode, error = await _run(argv, environment, timeout, f"the clone of {shown}")
    if returncode != 0:
        raise ScratchError(f"the clone of {shown} failed: {_complaint(error, token)}")
    _check_clean(destination, url, token)


def git_environment(
    base: Mapping[str, str], *, token: str | None = None, username: str = TOKEN_USERNAME
) -> dict[str, str]:
    """The environment git runs in: *base* made so that git never asks anyone anything.

    The daemon runs in the operator's session, and a git that wants a user
    name there would ask on the terminal or open the desktop's askpass
    dialog. So the terminal prompt is off, no askpass program is named, and
    the credential helpers of the operator's configuration are dropped:
    without the bot's token a clone that needs one fails at once. git
    configuration that *base* carries in variables is replaced. With
    *token*, git sends it with each request as the password of *username*.
    """
    environment = {
        key: value for key, value in base.items()
        if key not in _ASKING and not key.startswith("GIT_CONFIG_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["SSH_ASKPASS_REQUIRE"] = "never"
    settings = [("credential.helper", ""), ("core.askPass", "")]
    if token is not None:
        credential = base64.b64encode(f"{username}:{token}".encode()).decode()
        settings.append(("http.extraHeader", f"Authorization: Basic {credential}"))
    environment["GIT_CONFIG_COUNT"] = str(len(settings))
    for index, (key, value) in enumerate(settings):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


async def _run(
    argv: list[str], environment: dict[str, str], timeout: float, what: str
) -> tuple[int, bytes]:
    """Run *argv* to its end, and its exit status and error output.

    The process leads a group of its own, so the processes it starts can be
    stopped with it. When it outlives *timeout*, or the caller is cancelled,
    the group is killed and the process reaped before this returns.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    try:
        try:
            _, error = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            raise ScratchError(f"{what} did not finish in {timeout:g} seconds.") from None
    finally:
        if process.returncode is None:
            _kill_group(process.pid)
            await process.wait()
    return process.returncode, error


def _kill_group(pid: int) -> None:
    """Kill the process group that the process *pid* leads."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _check_clean(destination: Path, url: str, token: str | None) -> None:
    """Refuse a clone whose git configuration holds a credential."""
    config = Path(destination) / ".git" / "config"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScratchError(f"the clone has no git configuration to check ({exc.strerror}).") from None
    for line in text.splitlines():
        key, equals, value = line.partition("=")
        if not equals or key.strip().lower() not in ("url", "pushurl"):
            continue
        parts = urlsplit(value.strip())
        if parts.username is not None or parts.password is not None:
            raise ScratchError(f"the clone's remote URL {_shown(value.strip())} carries a credential.")
    if token and token in text:
        raise ScratchError("the clone's git configuration holds the forge credential.")


def _shown(url: str) -> str:
    """*url* as a fault may show it: without the user and password it may carry."""
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return parts._replace(netloc=host).geturl()


def _complaint(error: bytes, token: str | None) -> str:
    """The end of git's error output, without the credential, for a fault."""
    text = error.decode("utf-8", "replace").strip()
    if token:
        text = text.replace(token, "[credential]")
    text = text[-_GIT_ERROR_LIMIT:]
    return text or "git gave no reason."


def _remove(directory: Path) -> None:
    """Remove *directory* and all it holds, also what its run made read-only."""

    def writable(function, path, _exc):
        parent = os.path.dirname(path)
        os.chmod(parent, stat.S_IRWXU)
        if os.path.isdir(path) and not os.path.islink(path):
            os.chmod(path, stat.S_IRWXU)
        function(path)

    try:
        shutil.rmtree(directory, onexc=writable)
    except FileNotFoundError:
        pass
    except OSError:
        _log.exception("the run scratch dir %s could not be removed", directory)
