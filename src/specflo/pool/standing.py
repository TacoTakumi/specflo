"""Standing entries: what the live project agents take, for the ledger to count.

The daemon runs one agent for each hosted project, in the project's seat. It
is no member: no pool lists it, no lease started it, and it holds a
conversation over days that a lease's process could not. It runs on the same
rig and through the same accounts as the members do, though, so while it is
alive it stands in the ledger with what it takes (see ``ledger``).

What a project agent takes is not read off the agent. The daemon root's
configuration names it, once for every project agent of the root: a model of
the rig's llama-swap configuration, a provider account of the pool
configuration, or both. A root that names neither has no standing entries.

Whether an agent is alive is asked each time the entries are worked out,
and nothing is kept between two askings: an agent stopped from a shell is
gone from the ledger at the next request, and what it held is free. Which
projects have an agent, and the asking, are the daemon's and are handed in.
This module reaches no agent itself.

A standing entry is not a lease and the pool store has no row for it. It has
no idle limit, so the expiry check never meets it, and its id names no lease,
so ``end_lease`` refuses it as it refuses any id the store does not hold. It
ends when its agent does, by the project's own life cycle.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from .. import config as root_config
from ..daemon.poolstore import Resource
from . import ledger

# What a standing entry's id begins with; a lease's begins otherwise.
ID_PREFIX = "standing-"


def entry_id(project: str) -> str:
    """The id of the standing entry of the agent of *project*."""
    return ID_PREFIX + project


def entries(
    root: Path, agents: Mapping[str, str], alive: Callable[[str], bool]
) -> tuple[ledger.Standing, ...]:
    """The standing entries of the daemon root *root* now, in the order of *agents*.

    *agents* is every project's agent name by project, as the daemon has them
    on record, and *alive* says whether the agent of a project serves right
    now. One entry stands for each that does, with the resources the root's
    configuration names. When it names none, no agent is asked after.
    """
    use = root_config.project_agent_use(Path(root))
    resources = tuple(
        Resource(kind, name)
        for kind, name in ((ledger.MODEL, use.model), (ledger.ACCOUNT, use.account))
        if name is not None
    )
    if not resources:
        return ()
    return tuple(
        ledger.Standing(id=entry_id(project), project=project, agent=agent, resources=resources)
        for project, agent in agents.items()
        if alive(project)
    )
