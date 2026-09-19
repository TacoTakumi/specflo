"""The ``specflo console`` verbs: a developer's own agent as a member of a pool.

A pool's roster may declare a console: a slot with no process of the pool's
own. A developer who runs pi under an agent host on the daemon's host attaches
that host to the slot by name, and the pool then leases it like any member;
a detach makes the slot take no new lease. Both are asked of the daemon that
holds the pool, on the remote named or the only one registered, and the
daemon serves them to the developer identity alone. The verbs touch no agent
themselves: the agent runs where the daemon does, and the daemon binds it.

The command declarations are in the top-level CLI module, which every specflo
command loads; each one calls into this module when it runs. Like the lease
verbs, this module imports none of the pool's own code: the pool is on the
daemon.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .cli_lease import pick_remote


def attach(
    root: Path, slot: str, agent: str, *, remote: str | None = None, json_output: bool = False
) -> None:
    """Ask the daemon to attach the agent host *agent*, which runs on the
    daemon's host, to the console *slot*, and print what it answers.

    *root* is the checkout the remote is registered in. Raises
    ``SpecfloError`` with the words to print for anything that is refused,
    here or on the daemon.
    """
    registered = pick_remote(root, remote)
    from ..service.pool_remote import REQUEST_TIMEOUT, RemotePool

    client = RemotePool(registered.url, registered.token, timeout=REQUEST_TIMEOUT)
    attached = client.console_attach(slot, agent)
    if json_output:
        typer.echo(json.dumps({**attached, "remote": registered.name}))
        return
    typer.echo(
        f"Attached agent '{attached['agent']}' to console '{attached['slot']}' on remote"
        f" '{registered.name}': the pool leases it like any member until"
        f" `specflo console detach {attached['slot']}`."
    )


def detach(
    root: Path, slot: str, *, remote: str | None = None, json_output: bool = False
) -> None:
    """Ask the daemon to detach the console *slot*, and print the slot's state."""
    registered = pick_remote(root, remote)
    from ..service.pool_remote import REQUEST_TIMEOUT, RemotePool

    client = RemotePool(registered.url, registered.token, timeout=REQUEST_TIMEOUT)
    detached = client.console_detach(slot)
    if json_output:
        typer.echo(json.dumps({**detached, "remote": registered.name}))
        return
    typer.echo(
        f"Detached console '{detached['slot']}' on remote '{registered.name}': it takes no"
        f" new lease, and it is {detached['state']} now."
    )
