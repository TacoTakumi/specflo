"""The ``specflo agent`` command group: lifecycle verbs for pi subagents.

``start`` launches a detached host process (this module run with ``-m``) that
survives the invoking shell (REQ-01); ``status`` and ``list`` combine the
status.json snapshot with a live socket probe so a dead host is reported as
dead rather than echoing stale state (REQ-19). Names are unique among live
agents (REQ-18).

Exit codes (REQ-08), consistent across every verb:
    0 success, 10 busy, 11 timeout, 12 host unreachable / unknown agent,
    1 generic error.

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

from specflo.agent.client import HostUnreachableError, connect
from specflo.agent.statefiles import AgentPaths, default_base_dir, read_status

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_BUSY = 10
EXIT_TIMEOUT = 11
EXIT_UNREACHABLE = 12

DEFAULT_PI_CMD = "pi --mode rpc"

_PROBE_TIMEOUT = 2.0
_START_DEADLINE = 15.0

agent_app = typer.Typer(help="Run and control pi subagents (headless pi hosts).")


# -- probing ----------------------------------------------------------------


def _probe(name: str) -> dict:
    """One agent's live-checked view: socket answer first, disk as fallback.

    Returns {"name", "state", "alive", "status", "paths"} where "state" is
    the reported state: the live state when the host answers, "stopped" for
    a cleanly stopped agent, "dead" for an unreachable host with non-terminal
    disk state, and "unknown" when no state dir exists.
    """
    paths = AgentPaths.resolve(name)
    path_strs = {
        "socket": str(paths.socket),
        "events": str(paths.events),
        "status": str(paths.status),
    }
    try:
        with connect(name, connect_timeout=_PROBE_TIMEOUT) as client:
            data = client.status(timeout=_PROBE_TIMEOUT)
        status = data["status"]
        return {
            "name": name,
            "state": status["state"],
            "alive": True,
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
            "status": None,
            "paths": path_strs,
        }
    snapshot = read_status(paths.status)
    state = "stopped" if snapshot.get("state") == "stopped" else "dead"
    return {
        "name": name,
        "state": state,
        "alive": False,
        "status": snapshot,
        "paths": path_strs,
    }


def _echo_probe(probe: dict) -> None:
    status = probe["status"] or {}
    parts = [probe["name"], probe["state"]]
    if status.get("host_pid") is not None:
        parts.append(f"host_pid={status['host_pid']}")
    if status.get("pi_pid") is not None:
        parts.append(f"pi_pid={status['pi_pid']}")
    if status.get("last_activity"):
        parts.append(f"last_activity={status['last_activity']}")
    typer.echo("  ".join(parts))


# -- verbs ------------------------------------------------------------------


@agent_app.command(
    epilog="Example: specflo agent start builder --cwd ~/work/repo"
)
def start(
    name: str = typer.Argument(help="Agent name (unique among live agents)."),
    cwd: Path = typer.Option(
        Path("."), "--cwd", help="Directory the pi agent runs in."
    ),
    pi_cmd: str = typer.Option(
        DEFAULT_PI_CMD,
        "--pi-cmd",
        help="Command the host spawns and holds (default: the real pi in RPC mode).",
    ),
) -> None:
    """Start a detached agent host; it and its pi survive this invocation."""
    try:
        paths = AgentPaths.resolve(name)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        typer.echo(f"Error: --cwd {cwd} is not a directory", err=True)
        raise typer.Exit(code=EXIT_GENERIC)

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
    host_log = open(paths.root / "host.log", "ab")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "specflo.agent.cli",
            name,
            "--cwd",
            str(cwd),
            "--pi-cmd",
            pi_cmd,
        ],
        stdin=subprocess.DEVNULL,
        stdout=host_log,
        stderr=host_log,
        start_new_session=True,  # detach: survives this CLI and its shell
    )
    host_log.close()

    deadline = time.monotonic() + _START_DEADLINE
    while time.monotonic() < deadline:
        if proc.poll() is not None:
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
            typer.echo(
                f"started agent '{name}' (state {status['state']}, "
                f"host pid {status['host_pid']}, pi pid {status['pi_pid']})"
            )
            return
        except (HostUnreachableError, TimeoutError, RuntimeError, OSError):
            time.sleep(0.1)
    typer.echo(f"Error: agent '{name}' did not become ready in {_START_DEADLINE}s", err=True)
    raise typer.Exit(code=EXIT_TIMEOUT)


@agent_app.command(epilog="Example: specflo agent status builder --json")
def status(
    name: str = typer.Argument(help="Agent name."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Live-checked status: REQ-05 fields plus the state-dir paths."""
    try:
        probe = _probe(name)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
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
    probes = [_probe(name) for name in names]
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
    "12 host unreachable or unknown agent, 1 generic error."
)


def _connect_or_exit(name: str):
    try:
        return connect(name, connect_timeout=_PROBE_TIMEOUT)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    except HostUnreachableError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNREACHABLE)


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


def _print_last_text(client) -> None:
    response = client.request({"type": "get_last_assistant_text"}, timeout=10.0)
    if not response.get("success"):
        typer.echo(
            f"Error: get_last_assistant_text failed: {response.get('error')}",
            err=True,
        )
        raise typer.Exit(code=EXIT_GENERIC)
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
) -> None:
    """Send a prompt; block until settle and print the final assistant text."""
    if steer and follow_up:
        typer.echo("Error: --steer and --follow-up are mutually exclusive", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    with _connect_or_exit(name) as client:
        status = client.status(timeout=_PROBE_TIMEOUT)["status"]
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
            typer.echo(f"Error: prompt refused: {response.get('error')}", err=True)
            raise typer.Exit(code=EXIT_BUSY)
        if no_wait:
            typer.echo("submitted; not waiting for settle", err=True)
            return
        _wait_for_settle(client, timeout)
        _print_last_text(client)


@agent_app.command(epilog=f"Example: specflo agent wait builder\n\n{EXIT_CODES_HELP}")
def wait(
    name: str = typer.Argument(help="Agent name."),
    timeout: Optional[float] = typer.Option(
        None, "--timeout", help="Bound the wait, in seconds."
    ),
) -> None:
    """Block until the agent's current run settles (exit 0 if already idle)."""
    with _connect_or_exit(name) as client:
        status = client.status(timeout=_PROBE_TIMEOUT)["status"]
        if status["state"] != "working":
            return  # nothing in flight
        _wait_for_settle(client, timeout)


@agent_app.command(epilog=f"Example: specflo agent last builder\n\n{EXIT_CODES_HELP}")
def last(
    name: str = typer.Argument(help="Agent name."),
) -> None:
    """Print the most recent final assistant text."""
    with _connect_or_exit(name) as client:
        _print_last_text(client)


@agent_app.command(epilog=f"Example: specflo agent stop builder\n\n{EXIT_CODES_HELP}")
def stop(
    name: str = typer.Argument(help="Agent name."),
    timeout: float = typer.Option(
        30.0, "--timeout", help="Bound the wait for the host to shut down."
    ),
) -> None:
    """Gracefully stop an agent: abort its run, terminate pi, then the host."""
    try:
        paths = AgentPaths.resolve(name)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
    try:
        client = connect(name, connect_timeout=_PROBE_TIMEOUT)
    except HostUnreachableError as exc:
        if paths.status.exists() and read_status(paths.status).get("state") == "stopped":
            typer.echo(f"agent '{name}' is already stopped")
            return
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNREACHABLE)
    with client:
        response = client.stop(timeout=timeout)
        if not response.get("success"):
            typer.echo(f"Error: stop refused: {response.get('error')}", err=True)
            raise typer.Exit(code=EXIT_GENERIC)

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


@agent_app.command(epilog=f"Example: specflo agent log builder --follow\n\n{EXIT_CODES_HELP}")
def log(
    name: str = typer.Argument(help="Agent name."),
    follow: bool = typer.Option(
        False, "--follow", help="Keep streaming new events as they land."
    ),
) -> None:
    """Print the agent's event log (events.jsonl)."""
    try:
        paths = AgentPaths.resolve(name)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=EXIT_GENERIC)
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


def run_host(name: str, cwd: str, pi_cmd: str) -> None:
    """Run one agent host in the foreground until its stop verb fires."""
    from specflo.agent.host import PiHost

    host = PiHost(name, shlex.split(pi_cmd), cwd=cwd).start().serve()
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
    args = parser.parse_args(argv)
    run_host(args.name, args.cwd, args.pi_cmd)


if __name__ == "__main__":
    _host_main(sys.argv[1:])
