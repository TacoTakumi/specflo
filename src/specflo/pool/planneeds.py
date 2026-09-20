"""A hosted plan's Needs lines, read against the daemon's pools.

A plan counts pools of its own: a slot to each of its tasks under way. That
count is one plan's, so two projects on one daemon would both claim the same
members by it. A project the daemon hosts is read against the daemon instead:
a Needs name that is one of its pools has the daemon's size, and as many
slots taken as the pool has leases out, whichever project or orchestrator
holds them. The plan module takes that as a plain mapping and knows nothing
of where it came from; this module makes the mapping.

It runs on the daemon, from the root it serves, and only for a hosted
project: a plan in a checkout never loads it. It reads the pool directory and
the lease rows and changes neither.

A lease whose idle limit has passed is not out, though its row still says
active: the pool ends it only when it is next asked for something, and an
orchestrator that is told the pool is full asks it for nothing. So the leases
are judged here by the pool's own rule, on the rows and on what the members'
agent hosts say of themselves, and one that is due is left out of the count.
It is only left out: this module ends no lease and stops no member, and the
pool still does both at its next request.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..daemon.poolstore import open_pool_store
from . import expiry, ledger, runner
from .cli_admin import pool_dir
from .config import load_pool_config


def daemon_pools(root: Path | str, *, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """Each pool of the daemon root *root* by name: its ``size`` and the
    leases ``in_use``, as the pool status gives them: the active leases that
    have not been idle for their limit at the time *now*, the present when
    none is given.

    A root with no pool directory has no pools, and so has one whose
    configuration does not stand: the daemon serves no pool from it.
    """
    directory = pool_dir(Path(root))
    if not directory.is_dir():
        return {}
    config, errors = load_pool_config(directory)
    if errors:
        return {}
    if now is None:
        now = datetime.now(timezone.utc)
    with open_pool_store(Path(root)) as store:
        active = store.list_leases(state="active")
    # Every active lease is judged in one look: a team's leases lie in several
    # pools, and the latest activity among them renews them all.
    read = [(lease, runner.status(ledger.agent_of(lease))) for lease in active]
    due = {lease.id for lease in expiry.due(read, now)}
    return {
        pool.name: {
            "size": pool.size,
            "in_use": sum(1 for lease in active if lease.pool == pool.name and lease.id not in due),
        }
        for pool in config.pools
    }
