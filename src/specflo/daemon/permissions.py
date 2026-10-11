"""Who may do what on the daemon: one table from a role to its permissions.

A role is one row of :data:`ROLE_PERMISSIONS`, a set of named permissions,
and every access check asks :func:`can` whether the role at hand holds the
permission the surface needs; no check compares a role's name. A future role
is one new row holding whatever set it needs, and no check changes for it.

The developer holds every permission the requester holds, and the developer's
own besides: its row is built from the requester's, so that stays true when
the requester gains a permission. The agent is the built-in principal the
daemon runs; it holds only what its relay of a gate needs.

Access that is open to every signed-in principal, such as reading the
products or the bench's result batches, needs no permission and does not ask
the table: the sign-in check is the whole of it.
"""

from __future__ import annotations

# Manage the pool directory: the agent definitions and teams pages, and the
# reload that puts the directory in force.
POOL_MANAGE = "pool.manage"
# End a lease from the pool page, and be shown the control that does.
POOL_RELEASE = "pool.release"
# Attach a console to its slot, and detach it.
CONSOLE = "console"
# Read the bench's run records, and be shown their listing.
BENCH_RUNS = "bench.runs"
# Relay a gate's answer for the pipeline.
GATE_RELAY = "gate.relay"
# Pin a project's egress class: which models its requests may reach.
EGRESS_SET = "egress.set"
# Add a product, from the products page or the API.
PRODUCT_MANAGE = "product.manage"

PERMISSIONS = frozenset(
    {POOL_MANAGE, POOL_RELEASE, CONSOLE, BENCH_RUNS, GATE_RELAY, EGRESS_SET, PRODUCT_MANAGE}
)

_REQUESTER: frozenset[str] = frozenset()
_DEVELOPER = _REQUESTER | {POOL_MANAGE, POOL_RELEASE, CONSOLE, BENCH_RUNS, EGRESS_SET, PRODUCT_MANAGE}
_AGENT = frozenset({GATE_RELAY})

# A role to the permissions it holds, in the order a refusal names the roles.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "requester": _REQUESTER,
    "developer": _DEVELOPER,
    "agent": _AGENT,
}


def can(role: str, permission: str) -> bool:
    """Whether *role* holds *permission*.

    An unknown role holds nothing, and an unknown permission is held by no
    role: both answer False, so a check that names either refuses.
    """
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def who_can(permission: str) -> str:
    """The roles that hold *permission*, as a refusal names them: "the
    developer role", or "the developer or reviewer role" when more do."""
    roles = [role for role, held in ROLE_PERMISSIONS.items() if permission in held]
    return f"the {' or '.join(roles)} role" if roles else "no role"
