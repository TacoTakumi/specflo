"""The pool's audit records name the user who acted, and the role they acted under.

Every act on the pool that changes it appends an audit record: a lease asked
for and given back, a console attached and detached and a reload over the API,
and a definition saved and a lease released from the pool pages. The record
carries the user's own name beside the role, so two users who share a role are
told apart in the log; what each may do is still the role's matter.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from specflo.daemon import auth
from specflo.daemon.app import create_app
from specflo.service.pool_remote import (
    CONSOLE_ATTACH_PATH, CONSOLE_DETACH_PATH, LEASES_PATH, RELOAD_PATH,
)

from conftest import WEB_PASSWORD, web_sign_in

# The pool tests' rig and helpers, imported rather than copied. The rig's
# fixtures are named here so that pytest finds them from this module.
from pool.conftest import no_real_llama_swap, no_real_provider, pool_rig  # noqa: F401  (fixtures)
from pool.test_console_attach import AGENT, SLOT, start_host, write_console_pool
from pool.test_lease_request import audit_records, write_pool
from test_web_pool_definitions import FIELDS, edit_url, post
from test_web_pool_release import leased, release  # noqa: F401  (fixture)

USER, ROLE = "rob", "developer"


def rob(root) -> None:
    """The user rob, a developer, who can sign in on the web form too."""
    auth.add_user(root, USER, ROLE, WEB_PASSWORD)


def api(app) -> TestClient:
    """A client of *app* that asks with a personal token of rob's."""
    rob(app.state.root)
    client = TestClient(app)
    token = auth.mint_user_token(app.state.root, USER, "laptop")
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def browser(app) -> TestClient:
    """Rob's signed-in browser on *app*."""
    rob(app.state.root)
    client = TestClient(app, follow_redirects=False)
    web_sign_in(client, app.state.root, ROLE, USER)
    return client


def acted(root, operation: str) -> list[tuple[str, str]]:
    """Who each audit record of *operation* names, as (user, role)."""
    return [(r["user"], r["role"]) for r in audit_records(root) if r["operation"] == operation]


# -- over the API --------------------------------------------------------------------


def test_a_lease_request_and_its_release_are_audited_under_the_user(pool_rig):
    write_pool(pool_rig)
    # A daemon only asked over its routes serves no bridge of its own, and a
    # local member does not start without one.
    pool_rig.serve_bridge()
    client = api(create_app(pool_rig.root))

    asked = client.post(LEASES_PATH, json={"pool": "rebasers", "cwd": str(pool_rig.work)})
    assert asked.status_code == 200, asked.text
    granted = asked.json()["result"]
    assert acted(pool_rig.root, "lease_request") == [(USER, ROLE)]

    path = f"{LEASES_PATH}/{granted['lease_id']}/release"
    given_back = client.post(path, json={"token": granted["token"]})

    assert given_back.status_code == 200, given_back.text
    assert acted(pool_rig.root, "lease_release") == [(USER, ROLE)]


def test_a_console_attach_and_detach_are_audited_under_the_user(pool_rig):
    write_console_pool(pool_rig)
    start_host(pool_rig)
    client = api(create_app(pool_rig.root))

    attached = client.post(CONSOLE_ATTACH_PATH, json={"slot": SLOT, "agent": AGENT})
    assert attached.status_code == 200, attached.text
    detached = client.post(CONSOLE_DETACH_PATH, json={"slot": SLOT})
    assert detached.status_code == 200, detached.text

    assert acted(pool_rig.root, "console_attach") == [(USER, ROLE)]
    assert acted(pool_rig.root, "console_detach") == [(USER, ROLE)]


def test_a_reload_is_audited_under_the_user(pool_rig):
    write_pool(pool_rig)
    client = api(create_app(pool_rig.root))

    response = client.post(RELOAD_PATH, json={})

    assert response.status_code == 200, response.text
    assert acted(pool_rig.root, "pool_reload") == [(USER, ROLE)]


# -- on the pool pages ---------------------------------------------------------------


def test_a_definition_save_and_the_reload_it_makes_are_audited_under_the_user(pool_rig):
    write_pool(pool_rig)
    client = browser(create_app(pool_rig.root))
    # the pool's one member has no label, so the definition may need none
    fields = {**FIELDS, "prompt": "You are the rebaser. Name every conflict.", "needs": ""}

    response = post(client, edit_url("rebaser"), **fields)

    assert response.status_code == 303, response.text
    assert acted(pool_rig.root, "definition_save") == [(USER, ROLE)]
    assert acted(pool_rig.root, "pool_reload") == [(USER, ROLE)]


def test_a_release_from_the_pool_page_is_audited_under_the_user(pool_rig, leased):
    client = browser(leased["app"])

    response = release(client, leased["grant"].lease_id)

    assert response.status_code == 303, response.text
    assert acted(pool_rig.root, "lease_release") == [(USER, ROLE)]
