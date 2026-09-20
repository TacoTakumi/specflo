"""The pool page with a lease in the store whose agent's status cannot be read.

A store from before a member name that names no agent was refused may hold an
active lease under such a name. The agent host has no status for it, and the
page shows the lease by its row, as it shows that of any member with nothing
to go by.
"""

from __future__ import annotations

# The pool tests' rig and helpers, imported rather than copied. The rig's
# fixtures are named here so that pytest finds them from this module.
from pool.conftest import no_real_llama_swap, no_real_provider, pool_rig  # noqa: F401  (fixtures)
from pool.test_expiry import real_time, unreadable_lease
from test_web_pool import application, page, rows, signed_in


def test_the_page_shows_a_lease_whose_status_cannot_be_read(pool_rig):  # noqa: F811
    real_time(pool_rig)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    unread = unreadable_lease(pool_rig)
    pool_rig.clock.advance(minutes=4)

    html = page(signed_in(application(pool_rig, svc)))

    assert list(rows(html, "leases", "lease")) == [unread.id]
    assert "4m" in " ".join(rows(html, "leases", "lease")[unread.id])
