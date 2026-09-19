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
the lease rows and changes neither. A lease whose idle limit has passed still
counts until the pool ends it, which the pool does before it answers any
request of its own.
"""

from __future__ import annotations

from pathlib import Path

from ..daemon.poolstore import open_pool_store
from .cli_admin import pool_dir
from .config import load_pool_config


def daemon_pools(root: Path | str) -> dict[str, dict[str, int]]:
    """Each pool of the daemon root *root* by name: its ``size`` and the
    leases ``in_use``, as the pool status gives them.

    A root with no pool directory has no pools, and so has one whose
    configuration does not stand: the daemon serves no pool from it.
    """
    directory = pool_dir(Path(root))
    if not directory.is_dir():
        return {}
    config, errors = load_pool_config(directory)
    if errors:
        return {}
    with open_pool_store(Path(root)) as store:
        return {
            pool.name: {
                "size": pool.size,
                "in_use": len(store.list_leases(state="active", pool=pool.name)),
            }
            for pool in config.pools
        }
