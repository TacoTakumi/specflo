"""One lease's pi process: started and stopped through ``specflo agent``.

A member slot is long-lived; its pi process lives for one lease. Taking a
lease runs ``specflo agent start`` on the rpc transport, which places the
member's agent host in a herdr pane named for the member, so a person with
herdr access can watch any member. Ending the lease runs ``specflo agent
stop``. The agent subsystem is driven through its CLI, as a developer drives
it; the pool keeps no process handle, only the agent's name.

The agent host starts pi with its own environment, and under herdr that is
the pane's, not one the pool chose. So the host is not given pi's command
line. It is given a one-line command that runs this module, which reads what
the launch builder made - the command line and the scoped environment - from
one file only this user can read, removes the file, and becomes pi with that
environment and nothing else. The definition's prompt and the account's key
therefore never sit on a command line, in a pane's scrollback or, once pi
runs, on disk.

Once the host answers, the pool binds its own token and the lease's token on
it over the host's socket, which raises the lease wall before the holder is
told the agent's name. At the end the pool lowers the wall with its own
token, records why the lease ended in the agent's state directory and stops
the host; the host's exit ends the pane.

Agent names are one namespace on the daemon's host, so a host may answer
under a member's name that the pool did not start: a developer's own, started
by hand. A start that finds a host under the name refuses and touches
nothing. At the end, the token tells the pool's host from any other: the host
the pool started takes it, and one that does not take it is left running,
with nothing written beside it.

A console is the one member whose host the pool does not start. A developer
attaches an agent host that runs already: ``attach_console`` checks that one
answers under the name on the rpc transport and binds the pool's token on it,
and ``bind_console`` raises the wall on it for one lease. Neither starts
anything. A developer may have stopped the host since the attach and started
it again under the same name, and that host knows no pool: so ``bind_console``
makes the checks of an attach and binds the pool's token again before the
lease's. An agent on the TUI transport has no host to bind a token on, so it
is not attachable and takes no lease. The end of a console's lease is
``release_console`` and never ``stop``: the ending is recorded as for any
member, the wall is lowered and a turn the holder left running is aborted,
and the host and its pi run on, the developer's as they were.

A member's event log is the host's record of the lease, and the daemon reads
it for one thing: the turns that ended in a provider error. ``read_log`` gives
those errors' text and where the prompts stand in the log, and nothing else
that is in it; ``resend_prompt`` has the host forward one of those prompts
again, under the pool's token.

This is the one pool module that reaches the agent subsystem: its client, for
the wall's verbs, and its state directory layout. It talks to the agent host
and never to pi.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..agent import lease
from ..agent.client import HostUnreachableError, connect
from ..agent.statefiles import AgentPaths, read_status
from ..errors import SpecfloError
from . import launch, piconfig
from .config import Account, Member
from .definitions import AgentDefinition

_log = logging.getLogger(__name__)

# What pi is started from, in the agent's state directory: the command line
# and the environment, as JSON. It is there from just before the start until
# the member's pi reads it.
LAUNCH_FILE = "pool-launch.json"

# The path of the pi configuration directory generated for the lease, kept in
# the agent's state directory so that the end of the lease finds it again,
# from a fresh daemon process too. A local member has none.
CONFIG_DIR_FILE = "pool-config-dir"

# How long a start may take: the agent CLI gives the host's socket 15 s, and
# herdr placement comes before that.
START_TIMEOUT = 45.0
# How long the host may take to stop: it gives a running turn, then pi, a
# grace period each before it kills.
STOP_TIMEOUT = 30.0
# Added to a verb's own bound before the subprocess itself is given up on.
_CLI_GRACE = 15.0
_PROBE_TIMEOUT = 2.0
# Where the pool's own stop verb runs: no token file is found upward from it.
_NO_CHECKOUT = os.path.abspath(os.sep)


class RunnerError(SpecfloError):
    """A member's process that could not be started or stopped."""


class NotAttachable(SpecfloError):
    """An agent that cannot be attached as a console; the message says why,
    and names nothing of this host but the agent."""


def start(
    definition: AgentDefinition,
    member: Member,
    accounts: Iterable[Account],
    *,
    cwd: Path | str,
    pool_token: str,
    lease_token: str,
    config_root: Path | str,
    environ: Mapping[str, str] | None = None,
    timeout: float = START_TIMEOUT,
) -> str:
    """Start *member* for one lease in the role *definition* gives; the agent's name.

    pi runs in *cwd* with the launch builder's command line and scoped
    environment, the latter taken from *environ* (this process's by default).
    A hosted member's pi configuration directory is generated under
    *config_root*. The call returns once the host serves and the wall is up.

    Raises ``LaunchError`` for a member that cannot be launched as configured,
    and ``RunnerError`` for one already running or whose host does not come up
    or take the tokens; whatever such a start left behind is removed.
    """
    name = member.name
    paths = _paths(name)
    if _serving(name):
        # Found before anything is written: what is there belongs to whoever
        # started that host, a lease that runs or a developer, and so does
        # the host.
        raise RunnerError(
            f"member '{name}' cannot be started: an agent '{name}' already runs on the "
            "daemon's host, and this lease did not start it."
        )
    accounts = tuple(accounts)
    config_dir = piconfig.create(config_root, member, accounts)
    try:
        spec = {
            "argv": launch.pi_argv(definition, member),
            "env": launch.member_env(
                definition, member, accounts,
                os.environ if environ is None else environ,
                config_dir=config_dir,
            ),
        }
    except launch.LaunchError:
        piconfig.remove(config_dir)
        raise
    paths.ensure()
    lease.clear_ended(paths.root)
    _write_private(paths.root / LAUNCH_FILE, json.dumps(spec))
    if config_dir is not None:
        _write_private(paths.root / CONFIG_DIR_FILE, str(config_dir))
    pi_cmd = shlex.join([sys.executable, "-m", __name__, str(paths.root / LAUNCH_FILE)])
    try:
        started = _agent_cli(
            "start", name, "--transport", "rpc", "--cwd", str(cwd), "--pi-cmd", pi_cmd,
            timeout=timeout,
        )
        if started is None:
            raise RunnerError(f"member '{name}': its host did not answer in {timeout:.0f}s.")
        if started.returncode != 0:
            raise RunnerError(f"member '{name}' did not start: {_detail(started)}")
        try:
            with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
                client.pool_bind(pool_token)
                client.lease_bind(pool_token, lease_token)
        except (HostUnreachableError, TimeoutError, RuntimeError, OSError) as exc:
            raise RunnerError(f"member '{name}': its host did not take the lease: {exc}") from exc
    except RunnerError:
        # The CLI may have brought up a host that never answered in time and
        # left it running; it goes, so the name is free for the next start.
        _stop_cli(name, STOP_TIMEOUT)
        _forget(paths)
        raise
    return name


def stop(
    name: str,
    cause: str,
    *,
    pool_token: str,
    request_id: str | None = None,
    holder: str | None = None,
    timeout: float = STOP_TIMEOUT,
) -> None:
    """Stop the agent *name*; its lease ended for *cause*.

    The cause is one of the agent subsystem's lease end causes, and
    *request_id* names the preempting request. The record is written before
    the host is stopped, so a holder whose verb finds the host gone is told
    why. With *holder*, the hash of the lease's token, it is kept for that
    holder as well, so that it is told why when the member is leased again
    too. A host that is already gone is not an error: the lease ends all the
    same.

    A host that answers and does not take *pool_token* is not one this pool
    started: a start binds the token before anything else. It is someone
    else's under the member's name, a developer's or another pool's, and the
    lease never ran on it. It is left as it is, and so is its state
    directory: nothing is stopped and no record is written.

    Raises ``RunnerError`` when the host does not stop; what the lease left
    on disk then stays, because the member may still run.
    """
    paths = _paths(name)
    try:
        with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
            try:
                # The stop verb carries no pool token, so the wall comes down
                # first; and only the pool's own host takes the token.
                client.lease_clear(pool_token)
            except RuntimeError:
                # Said aloud, because the lease ends and the host runs on.
                _log.warning("agent %s does not take the pool's token: left running", name)
                return
    except (HostUnreachableError, TimeoutError, OSError):
        lease.write_ended(paths.root, cause, request_id, holder=holder)
        _forget(paths)
        return
    lease.write_ended(paths.root, cause, request_id, holder=holder)
    stopped = _stop_cli(name, timeout)
    if stopped is None or stopped.returncode != 0:
        detail = _detail(stopped) if stopped is not None else f"no answer in {timeout:.0f}s"
        raise RunnerError(f"member '{name}' did not stop: {detail}")
    _forget(paths)


def status(name: str) -> dict | None:
    """What the host of the agent *name* last wrote of itself: its state and
    ``last_activity``, the time its lease's holder last acted or its turn
    last showed life.

    None when there is nothing to go by: no status file, one that does not
    parse, or one whose host process is gone - a host killed mid-turn leaves
    a file that says working for ever.
    """
    try:
        record = read_status(_paths(name).status)
        os.kill(record["host_pid"], 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return record


def attach_console(name: str, *, pool_token: str) -> None:
    """Bind the pool's token on the agent host *name*, which runs already and
    which a developer attaches as a console. Nothing is started.

    Raises ``NotAttachable`` for a name that names no agent, an agent on the
    TUI transport, one whose host does not answer, and a host that does not
    take the token: a host takes one pool's token, again too, and no other's.
    """
    try:
        paths = AgentPaths.resolve(name)
    except ValueError as exc:
        raise NotAttachable(f"'{name}' cannot name an agent: {exc}") from exc
    try:
        # A TUI agent answers on no host's socket, so its record is read first.
        _refuse_tui(name, read_status(paths.status))
    except (OSError, ValueError):
        pass  # no record to go by; the socket says the rest
    try:
        with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
            _refuse_tui(name, client.status(timeout=_PROBE_TIMEOUT).get("status"))
            client.pool_bind(pool_token)
    except RuntimeError as exc:
        raise NotAttachable(
            f"agent '{name}': its host did not take the pool's token: {exc}. A host takes "
            "one pool's token and no other's; start a fresh agent host to attach."
        ) from exc
    except (HostUnreachableError, TimeoutError, OSError) as exc:
        raise NotAttachable(
            f"agent '{name}' is not running: no agent host answers under that name on the "
            f"daemon's host. Start it there with `specflo agent start {name}`."
        ) from exc


def bind_console(name: str, *, pool_token: str, lease_token: str) -> str:
    """Raise the lease wall on the attached console host *name* for one lease;
    the agent's name. Nothing is started.

    The host that answers may not be the one that was attached: a developer
    can stop it and start it again under the same name. So the pool's token is
    bound first, after the checks of an attach. A host that has the token
    takes it again, which changes nothing, and a fresh one is bound by it.

    Raises ``RunnerError`` for an agent on the TUI transport, and for a host
    that is gone, is bound to another pool or does not take the lease.
    """
    paths = _paths(name)
    try:
        try:
            # As at an attach: a TUI agent answers on no host's socket.
            _refuse_tui(name, read_status(paths.status))
        except (OSError, ValueError):
            pass  # no record to go by; the socket says the rest
        with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
            _refuse_tui(name, client.status(timeout=_PROBE_TIMEOUT).get("status"))
            client.pool_bind(pool_token)
            client.lease_bind(pool_token, lease_token)
    except (
        NotAttachable, HostUnreachableError, TimeoutError, RuntimeError, OSError
    ) as exc:
        raise RunnerError(f"console '{name}': its host did not take the lease: {exc}") from exc
    # As at a start: the last ending was a former lease's, not this one's.
    lease.clear_ended(paths.root)
    return name


def release_console(
    name: str,
    cause: str,
    *,
    pool_token: str,
    request_id: str | None = None,
    holder: str | None = None,
) -> None:
    """End the lease on the attached console host *name* for *cause*, and
    stop nothing: the host and its pi are the developer's.

    The record is written as ``stop`` writes it, so the former holder is told
    why. The wall comes down, and a turn that ran under the lease is aborted;
    pi stays up through an abort. A host that is gone, or that does not know
    the pool's token, is left alone: the lease ends all the same.
    """
    paths = _paths(name)
    lease.write_ended(paths.root, cause, request_id, holder=holder)
    try:
        with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
            # Read while the wall is up: a turn that runs now is the holder's.
            try:
                record = client.status(timeout=_PROBE_TIMEOUT).get("status")
            except (TimeoutError, RuntimeError):
                record = None  # the wall comes down all the same
            client.lease_clear(pool_token)
            if isinstance(record, dict) and record.get("state") == "working":
                client.request({"type": "abort"}, timeout=_PROBE_TIMEOUT)
    except (HostUnreachableError, TimeoutError, RuntimeError, OSError):
        # No host, or one that this pool is not bound on: nothing of it is
        # this pool's to clear or to abort.
        pass


def _refuse_tui(name: str, record: object) -> None:
    """Raise ``NotAttachable`` when *record*, a status record of the agent
    *name*, says the TUI transport. A record that names no transport is an
    rpc host's."""
    if isinstance(record, dict) and record.get("transport", "rpc") != "rpc":
        raise NotAttachable(
            f"agent '{name}' runs on the TUI transport, which has no agent host to hold a "
            "lease's wall; a console is an agent started on the rpc transport."
        )


@dataclass(frozen=True)
class LogRead:
    """What one read of a member's event log found. ``offset`` is where the
    next read starts and ``prompt_at`` where the last prompt the host forwarded
    is written. ``failures`` has one entry for each turn that ended in a
    provider error: where the prompt of that turn is written, and the error's
    text. None of it is the member's output."""

    offset: int
    prompt_at: int | None
    failures: tuple[tuple[int | None, str], ...]


def log_end(name: str) -> int:
    """Where the event log of the agent *name* ends now; 0 without one."""
    try:
        return _paths(name).events.stat().st_size
    except OSError:
        return 0


def lease_log_start(name: str) -> int:
    """Where the running host's part of the event log of *name* begins.

    The log outlives a lease, and a host is started for each one: its part
    begins at the last record of a host starting.
    """
    start = at = 0
    try:
        with open(_paths(name).events, "rb") as f:
            for line in f:
                event = _event(line)
                if event.get("type") == "host_state" and event.get("state") == "starting":
                    start = at
                at += len(line)
    except OSError:
        return 0
    return start


def read_log(name: str, offset: int, prompt_at: int | None = None) -> LogRead:
    """Read the event log of the agent *name* from *offset* on.

    *prompt_at* is what the read before this one gave. Only whole lines are
    taken; one still being written is left for the next read.
    """
    failures: list[tuple[int | None, str]] = []
    try:
        with open(_paths(name).events, "rb") as f:
            f.seek(offset)
            for line in f:
                if not line.endswith(b"\n"):
                    break
                event = _event(line)
                message = event.get("message")
                if event.get("type") == "host_forward" and event.get("command") == "prompt":
                    prompt_at = offset
                elif (
                    event.get("type") == "message_end"
                    and isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and message.get("stopReason") == "error"
                ):
                    failures.append((prompt_at, str(message.get("errorMessage") or "")))
                offset += len(line)
    except OSError:
        pass
    return LogRead(offset=offset, prompt_at=prompt_at, failures=tuple(failures))


def resend_prompt(name: str, prompt_at: int, *, pool_token: str) -> bool:
    """Have the host of *name* forward again the prompt written at *prompt_at*
    in its event log; whether the member took it.

    It goes in under the pool's token, which the lease wall admits and which
    does not count as the holder's activity. A member that is in a turn, or
    whose host is gone, does not take it.
    """
    try:
        with open(_paths(name).events, "rb") as f:
            f.seek(prompt_at)
            message = _event(f.readline()).get("message")
        if not isinstance(message, str):
            return False
        with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
            answer = client.request(
                {"type": "prompt", "message": message, "pool_token": pool_token}
            )
    except (HostUnreachableError, TimeoutError, OSError):
        return False
    return bool(answer.get("success"))


def _event(line: bytes) -> dict:
    """One line of an event log; an empty record for a line that is not one."""
    try:
        event = json.loads(line)
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}


def _paths(name: str) -> AgentPaths:
    try:
        return AgentPaths.resolve(name)
    except ValueError as exc:
        raise RunnerError(f"member '{name}' cannot name an agent: {exc}") from exc


def _serving(name: str) -> bool:
    """Does a host answer under this name right now?"""
    try:
        with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
            client.status(timeout=_PROBE_TIMEOUT)
    except (HostUnreachableError, TimeoutError, RuntimeError, OSError):
        return False
    return True


def _stop_cli(name: str, timeout: float) -> subprocess.CompletedProcess | None:
    """Run ``specflo agent stop`` as the pool, which holds no lease.

    The verb presents a lease token it finds in the environment or in a token
    file above its working directory, and a host turns away the token of a
    lease it has cleared. A daemon may run inside a holder's checkout, so the
    verb is given no such variable and a directory with no checkout above it.
    """
    environ = {k: v for k, v in os.environ.items() if k != lease.ENV_LEASE_TOKEN}
    return _agent_cli(
        "stop", name, "--timeout", str(timeout), timeout=timeout + _CLI_GRACE,
        cwd=_NO_CHECKOUT, env=environ,
    )


def _agent_cli(
    *args: str,
    timeout: float,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess | None:
    """Run one ``specflo agent`` verb as a subprocess of this interpreter, in
    this process's working directory and environment unless others are given.

    None when the verb outran *timeout*; the subprocess is then gone.
    """
    try:
        return subprocess.run(
            [sys.executable, "-c", "import sys; from specflo.cli import main; sys.exit(main())",
             "agent", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None


def _detail(done: subprocess.CompletedProcess) -> str:
    return (done.stderr or done.stdout).strip() or f"exit code {done.returncode}"


def _write_private(path: Path, text: str) -> None:
    """Write *text* to a new file that only this user can read."""
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _forget(paths: AgentPaths) -> None:
    """Remove what a lease left on disk: the launch file, if pi never read it,
    and the generated pi configuration directory."""
    (paths.root / LAUNCH_FILE).unlink(missing_ok=True)
    record = paths.root / CONFIG_DIR_FILE
    try:
        config_dir = record.read_text(encoding="utf-8").strip()
    except OSError:
        return
    piconfig.remove(config_dir or None)
    record.unlink(missing_ok=True)


# -- becoming pi (``python -m specflo.pool.runner <launch file>``) ----------


def _become_pi(launch_file: str) -> None:
    """Replace this process with the member's pi, as the launch file says."""
    path = Path(launch_file)
    spec = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    # The command is looked up on the member's own PATH, not this process's.
    os.execvpe(spec["argv"][0], spec["argv"], spec["env"])


if __name__ == "__main__":
    _become_pi(sys.argv[1])
