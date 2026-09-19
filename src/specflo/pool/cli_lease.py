"""The ``specflo lease`` verbs: what an orchestrator asks of a daemon's agent pool.

A pool is held by a daemon, so every verb here runs on a registered remote:
the one named, or the only one registered. A granted request leaves the
lease's token under the checkout, where the ``specflo agent`` verbs find it,
and tells the orchestrator the lease id and the name of the agent to drive.
The token is a credential: it is written for this user alone and never
printed.

The token is also what makes a lease this checkout's. The listing shows the
daemon the tokens kept here and prints the leases they hold, and a release
presents the token of the lease it names; the file goes when the lease has.

A request waits on the daemon for a full pool, and whoever runs the verb is
most often an orchestrator that reads its error stream. So the verb says
there, as soon as the daemon does, that the request waits, on what, in which
place and for how long, and an interrupt calls the request off: the
connection closes, and the daemon takes the request out of its queue.

The command declarations are in the top-level CLI module, which every specflo
command loads; each one calls into this module when it runs. What this module
needs of the rest of specflo it imports when a verb runs, and it imports none
of the pool's own code at all: the pool is on the daemon.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import threading
from pathlib import Path

import typer

from ..errors import SpecfloError

# An idle limit as the pool file writes one: a whole number and a unit.
_DURATION = re.compile(r"^([1-9][0-9]*)([smh])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}


def request(
    root: Path,
    pool: str,
    *,
    cwd: Path | None = None,
    idle_limit: str | None = None,
    label: str | None = None,
    wait: int,
    egress: str | None = None,
    remote: str | None = None,
    json_output: bool = False,
) -> None:
    """Ask a daemon for a member of *pool*, keep the lease's token, and print
    the lease id and the agent's name.

    *root* is the checkout the orchestrator works in. *wait* is how many
    seconds the request may wait on the daemon for a full pool; when they
    pass, the daemon's refusal names what the pool is full of. With 0 a
    full pool is refused at once. A request that has to wait says so once on
    the error stream, as text or with *json_output* as one JSON object, and
    SIGINT or SIGTERM calls it off. *egress* is the most open egress class of
    member the request takes; the daemon knows the classes, applies no-train
    without one, and refuses at once a pool with no member under it, however
    long the request may wait. The request names the active project when
    the daemon holds it, and the daemon holds the request to the class that
    project pins. Raises ``SpecfloError`` with the words to print for
    anything that is refused, here or on the daemon.
    """
    # A checkout with no daemon is refused before anything else is looked at.
    registered = pick_remote(root, remote)
    from ..service.pool_remote import REQUEST_TIMEOUT, RemotePool

    seconds = None if idle_limit is None else idle_seconds(idle_limit)
    directory = member_directory(cwd)
    # The answer comes after the wait, and the grant after that.
    client = RemotePool(registered.url, registered.token, timeout=REQUEST_TIMEOUT + wait)
    project = requesting_project(root, registered.name)

    def say_it_waits(notice) -> None:
        _say_it_waits(notice, remote=registered.name, json_output=json_output)

    try:
        with _interruptible():
            grant = client.request(
                pool, cwd=str(directory), idle_limit=seconds,
                label=label if label is not None else Path(root).name,
                wait=wait or None, egress=egress, project=project,
                on_waiting=say_it_waits,
            )
    except _Interrupted:
        # The connection is closed by now, and that is what tells the daemon.
        raise SpecfloError(
            f"The request for pool '{pool}' on remote '{registered.name}' was cancelled:"
            " it waits no longer."
        )
    token_path = store_token(root, grant.agent, grant.token)
    if json_output:
        typer.echo(json.dumps({
            "lease": grant.lease_id, "agent": grant.agent,
            "pool": pool, "remote": registered.name,
        }))
        return
    typer.echo(
        f"Leased '{grant.agent}' from pool '{pool}' on remote '{registered.name}':"
        f" lease {grant.lease_id}."
    )
    typer.echo(
        f"Drive it with `specflo agent prompt {grant.agent} <text>`;"
        f" its token is kept in {_shown(token_path, root)}."
    )


def _say_it_waits(notice, *, remote: str, json_output: bool) -> None:
    """Tell whoever reads the error stream that the request waits. The output
    stream is left to the result."""
    if json_output:
        typer.echo(json.dumps({
            "event": "waiting", "pool": notice.pool, "remote": remote,
            "full": notice.full, "place": notice.place, "wait": notice.wait,
        }), err=True)
        return
    typer.echo(
        f"Waiting for a member of pool '{notice.pool}' on remote '{remote}', for up to"
        f" {notice.wait} s: {notice.full} This request is number {notice.place} of those"
        " that wait for the pool. Interrupt the command to cancel the request.",
        err=True,
    )


class _Interrupted(BaseException):
    """SIGINT or SIGTERM came while the request was out. Not an ``Exception``:
    nothing between the socket and the verb takes it for a failure of its own."""


@contextlib.contextmanager
def _interruptible():
    """While the request is out, SIGINT and SIGTERM raise ``_Interrupted``
    where the verb waits. An orchestrator's background command may start with
    SIGINT ignored, and SIGTERM otherwise ends the process with no word. Only
    the main thread takes signals; elsewhere nothing changes."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupt(signum, frame):
        raise _Interrupted()

    caught = (signal.SIGINT, signal.SIGTERM)
    before = {number: signal.signal(number, interrupt) for number in caught}
    try:
        yield
    finally:
        for number, handler in before.items():
            # None stands for a handler that was not set from Python.
            signal.signal(number, signal.SIG_DFL if handler is None else handler)


def release(
    root: Path, lease_id: str, *, remote: str | None = None, json_output: bool = False
) -> None:
    """Give the lease *lease_id* back, forget its token, and print how it ended.

    The token is looked for among those kept under the checkout *root*. With
    none for this lease the daemon is asked all the same: a lease that has
    ended is reported as it ended, and one that is active is another's, which
    the daemon refuses.
    """
    registered = pick_remote(root, remote)
    from ..service.pool_remote import REQUEST_TIMEOUT, RemotePool

    client = RemotePool(registered.url, registered.token, timeout=REQUEST_TIMEOUT)
    kept = next(
        (path for path, lease in held_leases(root, client) if lease.lease_id == lease_id), None
    )
    ended = client.release(lease_id, token=None if kept is None else _token(kept))
    if kept is not None and ended.held:
        # The lease is over, so what proved its holder opens nothing now.
        kept.unlink(missing_ok=True)
    if json_output:
        typer.echo(json.dumps({"lease": ended.lease_id, "state": ended.state}))
        return
    typer.echo(f"Lease {ended.lease_id} on remote '{registered.name}': {ended.state}.")


def list_held(root: Path, *, remote: str | None = None, json_output: bool = False) -> None:
    """Print the leases this checkout holds: those the tokens under *root* hold."""
    registered = pick_remote(root, remote)
    from ..service.pool_remote import REQUEST_TIMEOUT, RemotePool

    client = RemotePool(registered.url, registered.token, timeout=REQUEST_TIMEOUT)
    leases = [lease for _path, lease in held_leases(root, client)]
    if json_output:
        typer.echo(json.dumps([
            {
                "lease": lease.lease_id, "agent": lease.agent, "pool": lease.pool,
                "state": lease.state, "acquired": lease.acquired,
                "last_activity": lease.last_activity, "idle_limit": lease.idle_limit,
            }
            for lease in leases
        ]))
        return
    if not leases:
        typer.echo(f"No leases held from remote '{registered.name}'.")
        return
    for lease in leases:
        typer.echo(
            f"{lease.lease_id}  {lease.agent}  pool '{lease.pool}'  {lease.state}"
            f"  acquired {lease.acquired}"
        )


def held_leases(root: Path, client) -> list[tuple[Path, object]]:
    """Each token file under the checkout *root* that holds a lease on the
    daemon behind *client*, with that lease. The daemon answers for the tokens
    it is shown, so no other orchestrator's lease is among them."""
    from ..agent import lease

    paths = sorted((Path(root) / lease.TOKEN_DIR).glob("*.token"))
    kept = [(path, token) for path in paths if (token := _token(path))]
    if not kept:
        return []
    answered = client.held([token for _, token in kept])
    return [(path, held) for (path, _), held in zip(kept, answered) if held is not None]


def _token(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def pick_remote(root: Path, remote: str | None):
    """The remote a lease verb runs on: the one named, or the only one registered."""
    from .. import config

    if remote is None:
        remotes = config.list_remotes(root)
        if not remotes:
            raise SpecfloError(
                "Pools need a daemon, and this checkout has no remote. Register the"
                " daemon that holds the pool with"
                " `specflo remote add <name> <url> --token <secret>`."
            )
        if len(remotes) > 1:
            raise SpecfloError(
                "More than one remote is registered (" + ", ".join(remotes)
                + "); pass --remote <name>."
            )
        (remote,) = remotes
    return config.load_remote(root, remote)


def requesting_project(root: Path, remote: str) -> str | None:
    """The project a request to the remote *remote* is made from: the
    checkout's active project, when that remote holds it; None with no active
    project, or one kept in the checkout.

    Only the slug goes out. The pin is read by the daemon from its own record
    of the project, so nothing sent from here can widen it. A project held by
    another remote is refused: the pool's daemon could not read its pin, and a
    request served with none might leave the class the project pins.
    """
    from .. import config

    slug = config.load_config(root).active_project
    holder = config.hosting_remote(root, slug) if slug else None
    if holder is not None and holder != remote:
        raise SpecfloError(
            f"The active project '{slug}' is held by remote '{holder}', and the pool is"
            f" asked of remote '{remote}', which cannot read the egress class the project"
            " pins. Ask the pool of the remote that holds the project."
        )
    return slug if holder is not None else None


def idle_seconds(text: str) -> int:
    """The idle limit *text* in seconds; it is written as in the pool file."""
    match = _DURATION.match(text.strip())
    if match is None:
        raise SpecfloError(
            f"An idle limit of '{text}' is not a duration: write a whole number of at"
            " least 1 and a unit, s, m or h, such as '10m'."
        )
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def member_directory(cwd: Path | None) -> Path:
    """Where the member starts: *cwd*, taken from the working directory when it
    is relative, or the working directory itself."""
    directory = Path.cwd() if cwd is None else (Path.cwd() / cwd)
    directory = Path(os.path.abspath(directory))
    if not directory.is_dir():
        raise SpecfloError(f"The working directory '{directory}' is not a directory.")
    return directory


def store_token(root: Path, agent: str, token: str) -> Path:
    """Keep the lease token of *agent* under the checkout *root*; the file's path.

    The agent verbs look for it upward from the working directory. The file
    and its directory are this user's alone, and nothing under the directory
    is ever committed.
    """
    from ..agent import lease

    # The name becomes a file name, and it came from a daemon.
    if not agent or Path(agent).name != agent:
        raise SpecfloError(f"The remote named an agent, '{agent}', that names no token file.")
    path = lease.token_file(root, agent)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    gitignore = path.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        # A file left by an earlier lease keeps the mode it had.
        os.fchmod(descriptor, 0o600)
        file.write(token + "\n")
    return path


def _shown(path: Path, root: Path) -> str:
    """*path* as the checkout spells it."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
