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
