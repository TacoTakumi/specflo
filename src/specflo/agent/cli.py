"""The ``specflo agent`` command group: lifecycle verbs for pi subagents.

``start`` launches a detached host process (this module run with ``-m``) that
survives the invoking shell (REQ-01); ``status`` and ``list`` combine the
status.json snapshot with a live socket probe so a dead host is reported as
dead rather than echoing stale state (REQ-19). Names are unique among live
agents (REQ-18).

Exit codes (REQ-08), consistent across every verb:
    0 success, 10 busy, 11 timeout, 12 host unreachable / unknown agent,
    1 generic error.

Under a pool lease the verbs that reach a member (prompt, wait, last, log,
status, reset, stop) present the holder's token, found by
``specflo.agent.lease``. Without the right token they exit 1 and show nothing
of the member; once the lease has ended they exit 12 naming the cause, when
its host is gone and also when the member is leased again and the token is
the ended lease's.

Imports nothing from specflo pipeline code (REQ-15) - only the agent
subsystem and typer.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from specflo.agent import lease, tui
from specflo.agent.client import HostUnreachableError, connect
from specflo.agent.herdr import HerdrAdapter, HerdrError
from specflo.agent.statefiles import (
    ENV_STATE_DIR,
    AgentPaths,
    default_base_dir,
    read_status,
)

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_BUSY = 10
EXIT_TIMEOUT = 11
EXIT_UNREACHABLE = 12
#: stop on an adopted session: detached (pi left running), distinct from 0.
EXIT_DETACHED = 13

DEFAULT_PI_CMD = "pi --mode rpc"

# herdr workspace label for agent tabs; the composition point (specflo.cli)
# bridges the pipeline `agent_space` config value into this env var.
AGENT_SPACE_ENV = "SPECFLO_AGENT_SPACE"
DEFAULT_AGENT_SPACE = "agents"

_PROBE_TIMEOUT = 2.0
_START_DEADLINE = 15.0


def _agent_space() -> str:
    return os.environ.get(AGENT_SPACE_ENV) or DEFAULT_AGENT_SPACE

agent_app = typer.Typer(help="Run and control pi subagents (headless pi hosts).")


# -- probing ----------------------------------------------------------------


def _transport_fields(snapshot: dict | None) -> dict:
    """Transport and ownership for a row (REQ-05).

    v2 records carry both fields; a v1 broker record predates them, and a v1
    agent is by definition the rpc transport under specflo's management.
    """
    if not snapshot:
        return {"transport": None, "ownership": None}
    return {
        "transport": snapshot.get("transport", "rpc"),
        "ownership": snapshot.get("ownership", "managed"),
    }


class _LeaseRefused(Exception):
    """The host's wall turned the verb away. Not a RuntimeError on purpose:
    the probe's except net must not read it as a dead host."""


def _probe(name: str, lease_token: str | None = None) -> dict:
    """One agent's live-checked view: socket answer first, disk as fallback.

    With no ``lease_token`` the probe is the bare liveness check every host
    answers. With one - even an empty one - the host checks it against a
    bound lease, and a refusal raises _LeaseRefused.

    Returns {"name", "state", "alive", "transport", "ownership", "status",
    "paths"} where "state" is the reported state: the live state when the
    host answers, "stopped" for a cleanly stopped agent, "dead" for an
    unreachable host with non-terminal disk state, and "unknown" when no
    record exists.
    """
    paths = AgentPaths.resolve(name)
    path_strs = {
        "socket": str(paths.socket),
        "events": str(paths.events),
        "status": str(paths.status),
    }
    try:
        with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
            if lease_token is None:
                data = client.status(timeout=_PROBE_TIMEOUT)
            else:
                data = _walled_status(client, lease_token)
        status = data["status"]
        return {
            "name": name,
            "state": status["state"],
            "alive": True,
            **_transport_fields(status),
            "status": status,
            "paths": data["paths"],
        }
    except (HostUnreachableError, TimeoutError, RuntimeError, OSError):
        pass
    if not paths.status.exists():
        return {
            "name": name,
            "state": "unknown",
            "alive": False,
            **_transport_fields(None),
            "status": None,
            "paths": path_strs,
        }
    snapshot = read_status(paths.status)
    state = "stopped" if snapshot.get("state") == "stopped" else "dead"
    return {
        "name": name,
        "state": state,
        "alive": False,
        **_transport_fields(snapshot),
        "status": snapshot,
        "paths": path_strs,
    }


def _walled_status(client, lease_token: str) -> dict:
    """The status payload, asked for with a credential so the wall checks it.

    A bare status frame is the open liveness probe; one that carries a
    lease_token is refused while a lease is bound and the token is not the
    holder's. An empty token is how a verb with none to present still learns
    that the member is leased. The host answers status itself, so the field
    never reaches pi, leased or not.
    """
    response = client.request(
        {"type": "status", "lease_token": lease_token}, timeout=_PROBE_TIMEOUT
    )
    if not response.get("success"):
        if lease.is_wall_refusal(response.get("error")):
            raise _LeaseRefused(response["error"])
        raise RuntimeError(f"status failed: {response.get('error')}")
    return response["data"]


def _echo_probe(probe: dict) -> None:
    status = probe["status"] or {}
    parts = [probe["name"], probe["state"]]
    if probe.get("transport"):
        parts.append(f"{probe['transport']} {probe['ownership']}")
    if status.get("host_pid") is not None:
        parts.append(f"host_pid={status['host_pid']}")
    if status.get("pi_pid") is not None:
        parts.append(f"pi_pid={status['pi_pid']}")
    if status.get("last_activity"):
        parts.append(f"last_activity={status['last_activity']}")
    typer.echo("  ".join(parts))


# -- verbs ------------------------------------------------------------------

_LEASE_TOKEN_OPTION = typer.Option(
    None,
    "--lease-token",
    help="The lease holder's token for a pooled agent (default: "
    f"${lease.ENV_LEASE_TOKEN}, else .specflo/leases/<name>.token found "
    "upward from the working directory).",
    show_default=False,
)


@agent_app.command(
    epilog="Example: specflo agent start builder --cwd ~/work/repo"
)
def start(
    name: str = typer.Argument(help="Agent name (unique among live agents)."),
    cwd: Path = typer.Option(
        Path("."), "--cwd", help="Directory the pi agent runs in."
    ),
    transport: str = typer.Option(
        "rpc",
        "--transport",
        help="Agent transport: 'rpc' (v1 broker host, default) or 'tui' "
        "(managed interactive pi in a herdr pane).",
    ),
    pi_cmd: str = typer.Option(
        DEFAULT_PI_CMD,
        "--pi-cmd",
        help="Command the host spawns and holds (default: the real pi in RPC mode).",
    ),
    no_auto_answer: bool = typer.Option(
        False,
        "--no-auto-answer",
        help="Disable the dialog auto-answer policy (dialogs wait for a controller).",
    ),
    workspace: Optional[str] = typer.Option(
        None,
        "--workspace",
        help="herdr workspace id for the agent tab (default: the agent_space workspace).",
    ),
    no_herdr: bool = typer.Option(
        False,
        "--no-herdr",
        help="Skip herdr placement and run headless even when herdr is available.",
    ),
) -> None:
    """Start a detached agent host; it and its pi survive this invocation."""
    if transport not in ("rpc", "tui"):
        typer.echo(
            f"Error: unknown transport '{transport}' (valid values: rpc, tui)",
            err=True,
        )
        raise typer.Exit(code=EXIT_GENERIC)
    try:
        paths = AgentPaths.resolve(name)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        typer.echo(f"Error: --cwd {cwd} is not a directory", err=True)
        raise typer.Exit(code=EXIT_GENERIC)

    if transport == "tui":
        # The managed TUI path (REQ-04): no host process - pi itself serves
        # the control socket through the specflo extension.
        _start_tui(name, cwd, workspace, pi_cmd, no_herdr)
        return

    if paths.socket.exists():
        # NB: typer.Exit subclasses RuntimeError - never raise it inside
        # this probe's except net
        try:
            with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
                client.status(timeout=_PROBE_TIMEOUT)
            live = True
        except (HostUnreachableError, TimeoutError, RuntimeError, OSError):
            live = False
        if live:
            typer.echo(
                f"Error: agent '{name}' is already running "
                f"(live host on {paths.socket})",
                err=True,
            )
            raise typer.Exit(code=EXIT_GENERIC)
        paths.socket.unlink(missing_ok=True)  # stale socket of a dead host

    paths.ensure()
    host_argv = [
        sys.executable,
        "-m",
        "specflo.agent.cli",
        name,
        "--cwd",
        str(cwd),
        "--pi-cmd",
        pi_cmd,
    ]
    if no_auto_answer:
        host_argv.append("--no-auto-answer")

    # herdr placement (REQ-11) or degraded headless (REQ-14, one warning)
    adapter = HerdrAdapter()
    if no_herdr:
        typer.echo(
            "warning: --no-herdr; running headless without herdr placement",
            err=True,
        )
        use_herdr = False
    elif not adapter.available():
        typer.echo(
            "warning: herdr unavailable; running headless without herdr placement",
            err=True,
        )
        use_herdr = False
    else:
        use_herdr = True

    proc = None
    if use_herdr:
        try:
            workspace_id = workspace or adapter.ensure_workspace(_agent_space())
            env = {}
            if os.environ.get(ENV_STATE_DIR):
                env[ENV_STATE_DIR] = os.environ[ENV_STATE_DIR]
            placement = adapter.create_tab(workspace_id, name, str(cwd), env=env)
            host_argv += [
                "--herdr-pane", placement.pane_id,
                "--herdr-workspace", placement.workspace_id,
                "--herdr-tab", placement.tab_id,
            ]
            # exec: the pane dies with the host, clearing the agent listing
            adapter.run_in_pane(placement.pane_id, "exec " + shlex.join(host_argv))
        except HerdrError as exc:
            typer.echo(f"Error: herdr placement failed: {exc}", err=True)
            raise typer.Exit(code=EXIT_GENERIC)
    else:
        host_log = open(paths.root / "host.log", "ab")
        proc = subprocess.Popen(
            host_argv,
            stdin=subprocess.DEVNULL,
            stdout=host_log,
            stderr=host_log,
            start_new_session=True,  # detach: survives this CLI and its shell
        )
        host_log.close()

    deadline = time.monotonic() + _START_DEADLINE
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            tail = (paths.root / "host.log").read_bytes()[-2000:].decode(errors="replace")
            typer.echo(
                f"Error: host process exited with code {proc.returncode} during start\n{tail}",
                err=True,
            )
            raise typer.Exit(code=EXIT_GENERIC)
        try:
            with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
                data = client.status(timeout=_PROBE_TIMEOUT)
            status = data["status"]
            where = (
                f", pane {status['herdr_pane']}" if status.get("herdr_pane") else ""
            )
            typer.echo(
                f"started agent '{name}' (state {status['state']}, "
                f"host pid {status['host_pid']}, pi pid {status['pi_pid']}{where})"
            )
            return
        except (HostUnreachableError, TimeoutError, RuntimeError, OSError):
            time.sleep(0.1)
    typer.echo(f"Error: agent '{name}' did not become ready in {_START_DEADLINE}s", err=True)
    raise typer.Exit(code=EXIT_TIMEOUT)


def _start_tui(
    name: str,
    cwd: Path,
    workspace: Optional[str],
    pi_cmd: str,
    no_herdr: bool,
) -> None:
    """The tui branch of ``start``: place pi in a pane, wait for its socket."""
    if no_herdr:
        typer.echo(
            "Error: --transport tui requires herdr; the pane is the transport",
            err=True,
        )
        raise typer.Exit(code=EXIT_GENERIC)
    adapter = HerdrAdapter()
    if not adapter.available():
        typer.echo("Error: --transport tui requires herdr, which is unavailable", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    # The rpc default names the broker's pi; the pane runs the interactive TUI.
    command = tui.DEFAULT_TUI_PI_CMD if pi_cmd == DEFAULT_PI_CMD else pi_cmd
    try:
        placement = tui.start_tui_agent(
            name,
            cwd,
            adapter,
            workspace=workspace,
            agent_space=_agent_space(),
            pi_cmd=command,
            timeout=tui.start_timeout(_START_DEADLINE),
        )
    except tui.TuiStartTimeout as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_TIMEOUT)
    except (tui.TuiStartError, HerdrError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    typer.echo(f"started agent '{name}' (tui, pane {placement.pane_id})")


@agent_app.command(epilog="Example: specflo agent status builder --json")
def status(
    name: str = typer.Argument(help="Agent name."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    lease_token: Optional[str] = _LEASE_TOKEN_OPTION,
) -> None:
    """Live-checked status: REQ-05 fields plus the state-dir paths."""
    token = lease.find_token(name, lease_token)
    try:
        probe = _probe(name, lease_token=token or "")
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    except _LeaseRefused as exc:
        _exit_refused(name, exc, token)
    if not probe["alive"]:
        _exit_if_lease_ended(name, token)
    if as_json:
        typer.echo(json.dumps(probe, indent=2))
    else:
        _echo_probe(probe)
    if probe["state"] == "unknown":
        if not as_json:
            typer.echo(f"Error: unknown agent '{name}'", err=True)
        raise typer.Exit(code=EXIT_UNREACHABLE)
    if probe["state"] == "dead":
        raise typer.Exit(code=EXIT_UNREACHABLE)


@agent_app.command("list")
def list_agents(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Every known agent with its live-checked state."""
    base = default_base_dir()
    names = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    # An "unknown" probe here is a husk: a cleanly exited v2 session retains
    # its events.jsonl but removes socket and record, leaving nothing to
    # attach to and nothing to report - pruned from the listing (REQ-18).
    probes = [p for p in (_probe(name) for name in names) if p["state"] != "unknown"]
    if as_json:
        typer.echo(json.dumps(probes, indent=2))
        return
    if not probes:
        typer.echo("no agents")
        return
    for probe in probes:
        _echo_probe(probe)


# -- prompt and retrieval verbs (REQ-06..REQ-09) ----------------------------

EXIT_CODES_HELP = (
    "Exit codes: 0 success/settled, 10 agent busy, 11 wait timeout, "
    "12 host unreachable or unknown agent, 13 adopted session detached (stop), "
    "1 generic error."
)


def _exit_refused(name: str, error, token: str | None = None) -> None:
    """The wall turned this verb away: say so, show nothing of the member.

    The member may be leased again since the lease of *token*, the one this
    verb presented, ended: its former holder is told how that lease ended, as
    it is when the host is gone. Any other token finds no record of its own
    and learns nothing of an ending."""
    if token:
        _exit_lease_ended(lease.read_ended(AgentPaths.resolve(name).root, token))
    typer.echo(
        f"Error: agent '{name}' is leased to another holder ({error}); "
        f"pass --lease-token, set {lease.ENV_LEASE_TOKEN}, or run from the "
        "directory tree that holds its token file",
        err=True,
    )
    raise typer.Exit(code=EXIT_GENERIC)


def _exit_if_lease_ended(name: str, token: str | None = None) -> None:
    """The host is gone: when the pool recorded why the lease ended, the
    former holder is told the cause rather than 'unreachable'. The record
    kept for *token* comes first: the last ending may be a later lease's."""
    root = AgentPaths.resolve(name).root
    _exit_lease_ended((token and lease.read_ended(root, token)) or lease.read_ended(root))


def _exit_lease_ended(record: dict | None) -> None:
    if record is not None:
        typer.echo(f"Error: {lease.ended_message(record)}", err=True)
        raise typer.Exit(code=EXIT_UNREACHABLE)


def _exit_failed(
    name: str, what: str, response: dict, code: int, token: str | None = None
) -> None:
    """A verb's frame came back unsuccessful; a wall refusal reads as one."""
    if lease.is_wall_refusal(response.get("error")):
        _exit_refused(name, response["error"], token)
    typer.echo(f"Error: {what}: {response.get('error')}", err=True)
    raise typer.Exit(code=code)


def _connect_or_exit(name: str, lease_token: str | None = None):
    try:
        return connect(
            name, connect_timeout=_PROBE_TIMEOUT, lease_token=lease_token
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    except HostUnreachableError as exc:
        _exit_if_lease_ended(name, lease_token)
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNREACHABLE)


def _status_or_exit(client, name: str, lease_token: str | None) -> dict:
    """The status snapshot, fetched through the wall (see _walled_status)."""
    try:
        return _walled_status(client, lease_token or "")["status"]
    except _LeaseRefused as exc:
        _exit_refused(name, exc, lease_token)


def _settled(frame) -> bool:
    return isinstance(frame, dict) and frame.get("type") == "agent_settled"


def _pi_gone(frame) -> bool:
    return isinstance(frame, dict) and frame.get("type") == "process_exit"


def _wait_for_settle(client, timeout: float | None) -> None:
    """Block until agent_settled; exit 11 on timeout, 12 when pi dies."""
    try:
        frame = client.read_until(
            lambda f: _settled(f) or _pi_gone(f), timeout=timeout
        )
    except TimeoutError:
        typer.echo(f"Error: agent did not settle within {timeout}s", err=True)
        raise typer.Exit(code=EXIT_TIMEOUT)
    except HostUnreachableError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNREACHABLE)
    if _pi_gone(frame):
        typer.echo("Error: pi process exited before settling", err=True)
        raise typer.Exit(code=EXIT_UNREACHABLE)


def _print_last_text(client, name: str) -> None:
    response = client.request({"type": "get_last_assistant_text"}, timeout=10.0)
    if not response.get("success"):
        _exit_failed(
            name, "get_last_assistant_text failed", response, EXIT_GENERIC, client.lease_token
        )
    text = (response.get("data") or {}).get("text")
    if text is not None:
        typer.echo(text)


@agent_app.command(
    epilog=f'Example: specflo agent prompt builder "run the tests"\n\n{EXIT_CODES_HELP}'
)
def prompt(
    name: str = typer.Argument(help="Agent name."),
    text: str = typer.Argument(help="The prompt text."),
    timeout: Optional[float] = typer.Option(
        None, "--timeout", help="Bound the wait for settle, in seconds."
    ),
    no_wait: bool = typer.Option(
        False, "--no-wait", help="Submit and return without waiting for settle."
    ),
    steer: bool = typer.Option(
        False,
        "--steer",
        help="Deliver into a running agent (pi streamingBehavior 'steer').",
    ),
    follow_up: bool = typer.Option(
        False,
        "--follow-up",
        help="Queue for after the current run (pi streamingBehavior 'followUp').",
    ),
    lease_token: Optional[str] = _LEASE_TOKEN_OPTION,
) -> None:
    """Send a prompt; block until settle and print the final assistant text."""
    if steer and follow_up:
        typer.echo("Error: --steer and --follow-up are mutually exclusive", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    token = lease.find_token(name, lease_token)
    with _connect_or_exit(name, token) as client:
        status = _status_or_exit(client, name, token)
        if status["state"] in ("exited", "stopped"):
            typer.echo(
                f"Error: agent '{name}' is {status['state']}; pi is not running",
                err=True,
            )
            raise typer.Exit(code=EXIT_UNREACHABLE)
        if status["state"] == "working" and not (steer or follow_up):
            typer.echo(
                f"Error: agent '{name}' is busy (working); "
                "use --steer or --follow-up to deliver anyway",
                err=True,
            )
            raise typer.Exit(code=EXIT_BUSY)

        command = {"type": "prompt", "message": text}
        if steer:
            command["streamingBehavior"] = "steer"
        elif follow_up:
            command["streamingBehavior"] = "followUp"
        response = client.request(command, timeout=10.0)
        if not response.get("success"):
            _exit_failed(name, "prompt refused", response, EXIT_BUSY, token)
        if no_wait:
            typer.echo("submitted; not waiting for settle", err=True)
            return
        _wait_for_settle(client, timeout)
        _print_last_text(client, name)


@agent_app.command(epilog=f"Example: specflo agent wait builder\n\n{EXIT_CODES_HELP}")
def wait(
    name: str = typer.Argument(help="Agent name."),
    timeout: Optional[float] = typer.Option(
        None, "--timeout", help="Bound the wait, in seconds."
    ),
    lease_token: Optional[str] = _LEASE_TOKEN_OPTION,
) -> None:
    """Block until the agent's current run settles (exit 0 if already idle)."""
    token = lease.find_token(name, lease_token)
    with _connect_or_exit(name, token) as client:
        # through the wall first: a leased host streams a member's events
        # only to a connection that has shown a valid token, and this frame
        # is what shows it
        status = _status_or_exit(client, name, token)
        if status["state"] != "working":
            return  # nothing in flight
        _wait_for_settle(client, timeout)


@agent_app.command(epilog=f"Example: specflo agent last builder\n\n{EXIT_CODES_HELP}")
def last(
    name: str = typer.Argument(help="Agent name."),
    lease_token: Optional[str] = _LEASE_TOKEN_OPTION,
) -> None:
    """Print the most recent final assistant text."""
    with _connect_or_exit(name, lease.find_token(name, lease_token)) as client:
        _print_last_text(client, name)


@agent_app.command(epilog=f"Example: specflo agent reset builder\n\n{EXIT_CODES_HELP}")
def reset(
    name: str = typer.Argument(help="Agent name."),
    lease_token: Optional[str] = _LEASE_TOKEN_OPTION,
) -> None:
    """Clear the agent's conversation context in place (pi new_session).

    The pi process, its working directory, its model and the prompt it was
    started with all stay; only what it was told since is gone.
    """
    token = lease.find_token(name, lease_token)
    with _connect_or_exit(name, token) as client:
        status = _status_or_exit(client, name, token)
        if status["state"] in ("exited", "stopped"):
            typer.echo(
                f"Error: agent '{name}' is {status['state']}; pi is not running",
                err=True,
            )
            raise typer.Exit(code=EXIT_UNREACHABLE)
        if status["state"] == "working":
            typer.echo(
                f"Error: agent '{name}' is busy (working); "
                "wait for the run to settle before a reset",
                err=True,
            )
            raise typer.Exit(code=EXIT_BUSY)
        response = client.request({"type": "new_session"}, timeout=10.0)
        if not response.get("success"):
            _exit_failed(name, "reset refused", response, EXIT_GENERIC, token)
        if (response.get("data") or {}).get("cancelled"):
            typer.echo(
                f"Error: reset of agent '{name}' was cancelled by a pi extension; "
                "its context is kept",
                err=True,
            )
            raise typer.Exit(code=EXIT_GENERIC)
    typer.echo(f"reset agent '{name}'")


@agent_app.command(epilog=f"Example: specflo agent stop builder\n\n{EXIT_CODES_HELP}")
def stop(
    name: str = typer.Argument(help="Agent name."),
    timeout: float = typer.Option(
        30.0, "--timeout", help="Bound the wait for the host to shut down."
    ),
    lease_token: Optional[str] = _LEASE_TOKEN_OPTION,
) -> None:
    """Gracefully stop an agent: abort its run, terminate pi, then the host."""
    try:
        paths = AgentPaths.resolve(name)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    # Ownership-aware stop for tui-transport sessions (REQ-06): the record
    # says what this is; the v1 broker path below stays untouched.
    if paths.status.exists():
        snapshot = read_status(paths.status)
        if snapshot.get("transport") == "tui":
            _stop_tui(name, snapshot, timeout)
            return
    token = lease.find_token(name, lease_token)
    try:
        client = connect(name, connect_timeout=_PROBE_TIMEOUT, lease_token=token)
    except HostUnreachableError as exc:
        _exit_if_lease_ended(name, token)
        if paths.status.exists() and read_status(paths.status).get("state") == "stopped":
            typer.echo(f"agent '{name}' is already stopped")
            return
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNREACHABLE)
    with client:
        response = client.stop(timeout=timeout)
        if not response.get("success"):
            _exit_failed(name, "stop refused", response, EXIT_GENERIC, token)

    def down() -> bool:
        if not paths.status.exists():
            return False
        snapshot = read_status(paths.status)
        if snapshot.get("state") != "stopped":
            return False
        host_pid = snapshot.get("host_pid")
        if host_pid:
            try:
                os.kill(host_pid, 0)
                return False  # host process still up
            except OSError:
                pass
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if down():
            typer.echo(f"stopped agent '{name}'")
            return
        time.sleep(0.1)
    typer.echo(f"Error: agent '{name}' did not stop within {timeout}s", err=True)
    raise typer.Exit(code=EXIT_TIMEOUT)


def _stop_tui(name: str, snapshot: dict, timeout: float) -> None:
    """The tui branch of ``stop``: kill managed, detach adopted (REQ-06)."""
    if snapshot.get("ownership") == "managed":
        # Release the pane registration when one is recorded; best-effort -
        # a herdr hiccup must not block ending the process.
        pane = snapshot.get("herdr_pane")
        if pane:
            # The extension registered the pane under its own source; the
            # release must match it or herdr refuses the authority change.
            adapter = HerdrAdapter(source=tui.EXTENSION_HERDR_SOURCE)
            if adapter.available():
                try:
                    adapter.release(pane, name)
                except HerdrError as exc:
                    typer.echo(f"warning: herdr release failed: {exc}", err=True)
        if not tui.stop_managed_tui(name, snapshot, timeout=timeout):
            typer.echo(
                f"Error: agent '{name}' did not stop within {timeout}s", err=True
            )
            raise typer.Exit(code=EXIT_TIMEOUT)
        typer.echo(f"stopped agent '{name}'")
        return
    # Adopted: the user's own pi is never specflo's to kill. Detach - remove
    # the registration - and say so distinctly.
    tui.detach_session(name)
    typer.echo(f"detached session '{name}' (pi left running)")
    raise typer.Exit(code=EXIT_DETACHED)


@agent_app.command(epilog=f"Example: specflo agent log builder --follow\n\n{EXIT_CODES_HELP}")
def log(
    name: str = typer.Argument(help="Agent name."),
    follow: bool = typer.Option(
        False, "--follow", help="Keep streaming new events as they land."
    ),
    lease_token: Optional[str] = _LEASE_TOKEN_OPTION,
) -> None:
    """Print the agent's event log (events.jsonl)."""
    try:
        paths = AgentPaths.resolve(name)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    # The log is a file, so the wall is asked before it is read: a live host
    # under a lease refuses anyone but the holder. A host that is gone has no
    # wall; its log stays readable unless the pool recorded a lease ending.
    token = lease.find_token(name, lease_token)
    try:
        client = connect(name, connect_timeout=_PROBE_TIMEOUT, lease_token=token)
    except HostUnreachableError:
        _exit_if_lease_ended(name, token)
    else:
        # NB: typer.Exit subclasses RuntimeError - the refusal is raised
        # outside this except net, never inside it
        refusal = None
        with client:
            try:
                _walled_status(client, token or "")
            except _LeaseRefused as exc:
                refusal = exc
            except (TimeoutError, RuntimeError, OSError):
                pass  # a host too sick to answer guards nothing
        if refusal is not None:
            _exit_refused(name, refusal, token)
    if not paths.events.exists():
        typer.echo(f"Error: unknown agent '{name}' (no event log)", err=True)
        raise typer.Exit(code=EXIT_UNREACHABLE)
    with open(paths.events, "rb") as f:
        while True:
            chunk = f.read(65536)
            if chunk:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                continue
            if not follow:
                return
            time.sleep(0.2)


# -- detached host entry (``python -m specflo.agent.cli``) ------------------


def run_host(
    name: str,
    cwd: str,
    pi_cmd: str,
    auto_answer: bool = True,
    herdr_pane: str | None = None,
    herdr_workspace: str | None = None,
    herdr_tab: str | None = None,
) -> None:
    """Run one agent host in the foreground until its stop verb fires."""
    from specflo.agent.host import PiHost
    from specflo.agent.transcript import TranscriptRenderer

    host = (
        PiHost(
            name,
            shlex.split(pi_cmd),
            cwd=cwd,
            auto_answer=auto_answer,
            herdr_pane=herdr_pane,
            herdr_workspace=herdr_workspace,
            herdr_tab=herdr_tab,
            # stdout is the pane under herdr, host.log when headless (REQ-12)
            transcript=TranscriptRenderer(sys.stdout),
        )
        .start()
        .serve()
    )
    try:
        host.wait_stopped()
    finally:
        host.close()


def _host_main(argv: list[str]) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="specflo-agent-host")
    parser.add_argument("name")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--pi-cmd", default=DEFAULT_PI_CMD)
    parser.add_argument("--no-auto-answer", action="store_true")
    parser.add_argument("--herdr-pane", default=None)
    parser.add_argument("--herdr-workspace", default=None)
    parser.add_argument("--herdr-tab", default=None)
    args = parser.parse_args(argv)
    run_host(
        args.name,
        args.cwd,
        args.pi_cmd,
        auto_answer=not args.no_auto_answer,
        herdr_pane=args.herdr_pane,
        herdr_workspace=args.herdr_workspace,
        herdr_tab=args.herdr_tab,
    )


if __name__ == "__main__":
    _host_main(sys.argv[1:])
