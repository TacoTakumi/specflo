"""The pool page: what the agent pool holds now, for a signed-in browser to read.

The page is the dashboard's view of the daemon's pool: each pool with its size,
the leases out of it and the requests that wait for it; each member with its
state, what backs it and the model reloads seen on it; each lease with its
holder, how long it has been idle and how long it has left; each provider
account with its figures and whether it is open; what the project agents take
outside the pool; and the last things that happened to leases. Reading it runs
the lazy expiry check first, as every read of the pool's state does.

It shows nothing a member wrote, no token and no hash of one. With a pool
configuration that did not stand it lists the faults and nothing else of the
pool, and a root that declares no pool says so.

The pool's state is built through the pool service on the pool tests' rig, with
its clock and its helpers; the page is read through the daemon's application.
"""

from __future__ import annotations

import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import auth, pool_routes, pool_web, web
from specflo.daemon.app import create_app
from specflo.daemon.poolstore import AccountFigures, ConsoleAttachment, Reload, Transition
from specflo.pool import cli_admin
from specflo.pool import config as pool_config
from specflo.pool.service import Grant, hash_token
from specflo.service.pool_remote import HELD_PATH, LEASES_PATH, release_path

# The pool tests' rig and helpers, imported rather than copied. The rig's
# fixtures are named here so that pytest finds them from this module.
from pool.conftest import no_real_llama_swap, no_real_provider, pool_rig  # noqa: F401  (fixtures)
from pool.test_cli_validate import _write_three_faults
from pool.test_console_attach import console_member
from pool.test_events_reloads import T0, lease_on
from pool.test_expiry import at, prompt, text
from pool.test_lease_request import write_pool
from pool.test_preempt import one_of_two, row
from pool.test_preempt import stamps  # noqa: F401  (fixture)
from pool.test_runner import POOL_TOKEN
from pool.test_standing import entry
from pool.test_waiting import asks

POOL_PAGE = "/pool"
REOPEN = "2026-03-02T00:00:00.000+00:00"
UNAVAILABLE = "reload data unavailable"
NO_POOL = "No pool is configured on this daemon."
# What a member's pi answers in the test of what the pool pages never carry.
MARKER = "zebra-crossing-7731"


def application(pool_rig, svc=None):
    """The daemon on the rig's root; with *svc*, serving that pool service."""
    app = create_app(pool_rig.root)
    if svc is not None:
        app.state.pool = svc
    return app


def signed_in(app, identity: str = "developer") -> TestClient:
    client = TestClient(app, follow_redirects=False)
    token = auth.mint_token(app.state.root, identity)
    response = client.post(web.SIGNIN_PATH, data={"identity": identity, "token": token})
    assert response.status_code == 303, response.text
    return client


def page(client) -> str:
    response = client.get(POOL_PAGE)
    assert response.status_code == 200, response.text
    return response.text


def section(html: str, name: str) -> str:
    match = re.search(rf'<section id="{name}">(.*?)</section>', html, re.S)
    assert match, f"no {name} section"
    return match.group(1)


def rows(html: str, name: str, key: str) -> dict[str, list[str]]:
    """The rows of the section *name* that carry ``data-<key>``: each one's cells as text."""
    found = {}
    pattern = rf'<tr data-{key}="([^"]+)">(.*?)</tr>'
    for value, body in re.findall(pattern, section(html, name), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)
        found[unescape(value)] = [
            " ".join(unescape(re.sub(r"<[^>]+>", "", cell)).split()) for cell in cells
        ]
    return found


# -- the whole page, on a pool with something of everything ---------------------


@pytest.fixture
def busy(pool_rig, stamps):
    """Two pools with one lease out, one lease preempted, one request that
    waits, a closed account with figures, a project agent standing on that
    account and a reload seen on the leased member. The time is minute 8."""
    svc = pool_rig.service(one_of_two(pool_rig))
    svc.standing = lambda: (entry("account", "team-a"),)
    other = svc.grant("others", holder_label="x", cwd=pool_rig.work)
    old = svc.grant("rebasers", holder_label="orchestrator-a (developer)", cwd=pool_rig.work)
    assert old.agent == "hosted-1"
    svc.end_lease(other.lease_id, "released")
    pool_rig.clock.advance(minutes=6)
    second = asks(pool_rig, svc, "rebasers", "b")
    assert second.attempt() is None
    granted = second.attempt()
    assert granted is not None and granted.agent == "local-1"
    third = asks(pool_rig, svc, "others", "c")
    assert third.attempt() is None
    pool_rig.clock.advance(minutes=2)
    # the holder of the lease that is out did something at minute 7
    stamps["local-1"] = {"state": "idle", "last_activity": text(at(7))}
    with pool_rig.store() as store:
        store.set_account_figures("team-a", AccountFigures(
            usage=12.5, limit=20.0, remaining=7.5, free_requests=0,
            read_at=text(at(8)), read_error=None,
        ))
        store.set_account_closed("team-a", reopen=REOPEN)
        store.add_reload(Reload(id=0, member="local-1", model="tc3", time=text(at(7))))
        store.set_reload_data_available(True)
    yield {"svc": svc, "old": old, "granted": granted}
    svc.end_lease(granted.lease_id, "released")


def test_the_page_shows_each_pool_with_its_size_its_leases_out_and_its_waiting_requests(
    pool_rig, busy
):
    html = page(signed_in(application(pool_rig, busy["svc"])))

    pools = rows(html, "pools", "pool")
    assert list(pools) == ["rebasers", "others"]
    # name, size, in use, waiting, ...
    assert pools["rebasers"][:4] == ["rebasers", "1", "1", "0"]
    assert pools["others"][:4] == ["others", "1", "0", "1"]
    waiting = rows(html, "waiting", "waiting")
    assert list(waiting) == ["request-c"]
    assert "c" in waiting["request-c"] and "others" in waiting["request-c"]


def test_the_page_shows_each_member_with_its_state_its_backing_and_its_reloads(pool_rig, busy):
    html = page(signed_in(application(pool_rig, busy["svc"])))

    members = rows(html, "members", "member")
    assert list(members) == ["local-1", "hosted-1"]
    local, hosted = members["local-1"], members["hosted-1"]
    for shown in ("leased", "local", "tc3"):
        assert shown in local
    for shown in ("idle", "hosted", "no-train"):
        assert shown in hosted
    assert any("team-a" in cell for cell in hosted)
    # one reload was seen on local-1, of its model at minute 7, and none on hosted-1
    assert any(cell.startswith("1 ") and text(at(7)) in cell for cell in local)
    assert not any(text(at(7)) in cell for cell in hosted)
    assert UNAVAILABLE not in html


def test_the_page_shows_each_lease_with_its_holder_its_idle_time_and_its_time_to_expiry(
    pool_rig, busy
):
    html = page(signed_in(application(pool_rig, busy["svc"])))

    leases = rows(html, "leases", "lease")
    # the preempted lease is out no more
    assert list(leases) == [busy["granted"].lease_id]
    (lease,) = leases.values()
    for shown in ("b", "rebasers", "local-1", "active"):
        assert shown in lease
    # last touched at minute 7 by its host's stamp, read at minute 8, limit 10 minutes
    assert "1m" in lease and "9m" in lease


def test_the_page_shows_each_account_with_its_figures_and_open_or_closed(pool_rig, busy):
    html = page(signed_in(application(pool_rig, busy["svc"])))

    accounts = rows(html, "accounts", "account")
    assert list(accounts) == ["team-a", "team-b"]
    # name, cap, slots in use, usage, limit, remaining, open or closed
    assert accounts["team-a"][:6] == ["team-a", "2", "1", "12.5", "20", "7.5"]
    assert accounts["team-a"][6] == f"closed until {REOPEN}"
    assert accounts["team-b"][:3] == ["team-b", "2", "0"]
    assert accounts["team-b"][6] == "open"


def test_a_closed_account_whose_reopen_time_has_come_reads_as_open(pool_rig, busy):
    # nothing is written: the store keeps the account closed, and the clock opens it
    pool_rig.clock.now = pool_rig.clock.now.replace(day=2, hour=0, minute=1)

    html = page(signed_in(application(pool_rig, busy["svc"])))

    assert rows(html, "accounts", "account")["team-a"][6] == "open"


def test_the_page_shows_each_standing_entry_with_no_control(pool_rig, busy):
    html = page(signed_in(application(pool_rig, busy["svc"])))

    standing = rows(html, "standing", "standing")
    assert list(standing) == ["standing-login-fix"]
    (shown,) = standing.values()
    assert "login-fix" in shown and "project-login-fix" in shown
    assert any("team-a" in cell for cell in shown)
    body = section(html, "standing")
    assert "<form" not in body and "<button" not in body


def test_the_page_shows_the_recent_transitions_and_a_preempted_one_names_who_took_it(
    pool_rig, busy
):
    html = page(signed_in(application(pool_rig, busy["svc"])))

    transitions = list(rows(html, "transitions", "transition").values())
    kinds = [(cells[1], cells[2], cells[3]) for cells in transitions]
    # newest first: time, kind, lease, cause
    assert kinds[0] == ("granted", busy["granted"].lease_id, "requested")
    assert ("preempted", busy["old"].lease_id, "preempted by request-b") in kinds
    assert len(transitions) == 5


def test_the_page_carries_no_token_and_no_hash_of_one(pool_rig, busy):
    html = page(signed_in(application(pool_rig, busy["svc"])))

    assert busy["granted"].token not in html
    assert hash_token(busy["granted"].token) not in html
    assert busy["old"].token not in html
    assert hash_token(busy["old"].token) not in html
    assert POOL_TOKEN not in html


def test_the_transitions_shown_are_bounded_to_the_newest(pool_rig, stamps):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    with pool_rig.store() as store:
        for n in range(1, 16):
            lease_on(store, "local-1", f"lease-{n}")
            store.record_transition(
                Transition(id=0, lease_id=f"lease-{n}", kind="released", time=T0, cause="released"),
                expect="active",
            )

    html = page(signed_in(application(pool_rig, svc)))

    transitions = list(rows(html, "transitions", "transition").values())
    assert len(transitions) == 20
    assert transitions[0][1:3] == ["released", "lease-15"]


def test_what_an_agent_cli_printed_of_a_member_that_did_not_start_is_not_shown(pool_rig, stamps):
    # the service keeps the runner's words in the cause; they can name paths on
    # the daemon's host and repeat what the member's process wrote
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    detail = f"member 'local-1' did not start: /home/someone/pi: {MARKER}"
    with pool_rig.store() as store:
        lease_on(store, "local-1")
        store.record_transition(
            Transition(
                id=0, lease_id="lease-1", kind="released", time=T0,
                cause=f"member did not start: {detail}",
            ),
            expect="active",
        )

    html = page(signed_in(application(pool_rig, svc)))

    newest = list(rows(html, "transitions", "transition").values())[0]
    assert newest[1:3] == ["released", "lease-1"]
    assert newest[3].startswith("member did not start")
    assert "log" in newest[3]
    assert MARKER not in html and "/home/someone" not in html


# -- the lazy expiry check --------------------------------------------------------


def test_reading_the_page_ends_a_lease_that_is_past_its_idle_limit(pool_rig, stamps):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    client = signed_in(application(pool_rig, svc))
    pool_rig.clock.advance(minutes=9)
    assert list(rows(page(client), "leases", "lease")) == [grant.lease_id]

    # nothing but the time moves, and nothing but the page is read
    pool_rig.clock.advance(minutes=2)
    html = page(client)

    assert rows(html, "leases", "lease") == {}
    newest = list(rows(html, "transitions", "transition").values())[0]
    assert newest[1:3] == ["expired", grant.lease_id]
    assert rows(html, "members", "member")["local-1"][2] == "idle"
    with pool_rig.store() as store:
        assert store.get_lease(grant.lease_id).state == "expired"


# -- a team, a console, the reload reader -----------------------------------------


def test_a_member_lease_of_a_team_shows_the_team_id_and_is_judged_by_the_teams_activity(
    pool_rig, stamps
):
    local, hosted = pool_rig.local_member(), pool_rig.hosted_member()
    svc = pool_rig.service(pool_rig.config(local, hosted))
    with pool_rig.store() as store:
        # an hour's lease on each member, of one team, granted at minute 0
        store.add_lease(row("local-1", "rebasers", team="team-lease-1"))
        store.add_lease(row("hosted-1", "rebasers", team="team-lease-1"))
    pool_rig.clock.advance(minutes=5)
    # a prompt to one member at minute 4 keeps the other as well
    stamps["hosted-1"] = {"state": "idle", "last_activity": text(at(4))}

    leases = rows(page(signed_in(application(pool_rig, svc))), "leases", "lease")

    for cells in leases.values():
        assert "team-lease-1" in cells
        assert "1m" in cells and "59m" in cells


def test_a_console_is_offline_idle_leased_or_draining(pool_rig, stamps):
    slot = console_member()
    svc = pool_rig.service(pool_rig.config(slot))
    client = signed_in(application(pool_rig, svc))

    def state() -> str:
        return rows(page(client), "members", "member")[slot.name][2]

    assert state() == "offline"
    with pool_rig.store() as store:
        store.attach_console(
            ConsoleAttachment(slot=slot.name, agent="rob-pi", attached=text(at(0)))
        )
    stamps["rob-pi"] = {"state": "idle", "last_activity": text(at(0))}
    assert state() == "idle"
    with pool_rig.store() as store:
        lease_on(store, slot.name)
    assert state() == "leased"
    with pool_rig.store() as store:
        store.set_console_draining(slot.name)
    assert state() == "draining"
    # a host that is gone serves no one, whatever its row says
    del stamps["rob-pi"]
    assert state() == "offline"


def test_the_page_says_when_the_reload_reader_is_down(pool_rig, stamps):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    client = signed_in(application(pool_rig, svc))

    assert UNAVAILABLE in page(client)

    with pool_rig.store() as store:
        store.set_reload_data_available(True)
    assert UNAVAILABLE not in page(client)


# -- no pool to show ----------------------------------------------------------------


def test_with_a_configuration_that_did_not_stand_the_page_lists_every_fault(tmp_path):
    root = _write_three_faults(tmp_path / "daemon")
    _, faults = pool_config.load_pool_config(cli_admin.pool_dir(root))
    assert len(faults) == 3

    html = page(signed_in(create_app(root)))

    shown = unescape(html)
    for fault in faults:
        assert str(fault) in shown
    assert '<section id="pools">' not in html
    assert NO_POOL not in shown


def test_a_root_with_no_pool_directory_says_that_no_pool_is_configured(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")

    html = page(signed_in(create_app(root)))

    assert NO_POOL in html
    assert '<section id="pools">' not in html


# -- who reads it, and how it is reached -------------------------------------------


def test_a_browser_with_no_session_is_sent_to_sign_in(tmp_path):
    client = TestClient(create_app(tmp_path / "daemon"), follow_redirects=False)

    response = client.get(POOL_PAGE)

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH


def test_the_requester_reads_the_page_as_the_developer_does(pool_rig, stamps):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    html = page(signed_in(application(pool_rig, svc), "requester"))

    assert list(rows(html, "pools", "pool")) == ["rebasers"]


def test_the_navigation_links_the_page_for_a_signed_in_identity_only(tmp_path):
    app = create_app(tmp_path / "daemon")
    link = f'href="{POOL_PAGE}"'

    assert link not in TestClient(app).get(web.SIGNIN_PATH).text
    home = signed_in(app).get(web.HOME_PATH)
    assert home.status_code == 200
    assert link in re.search(r"<header>(.*?)</header>", home.text, re.S).group(1)


def test_the_only_route_that_changes_the_pool_is_the_release_of_a_lease():
    routes = {route.path: route.methods for route in pool_web.pages.routes}

    assert routes == {POOL_PAGE: {"GET"}, pool_web.RELEASE_PATH: {"POST"}}
    assert web.POOL_PATH == POOL_PAGE


# -- nothing a member wrote ----------------------------------------------------------


def test_what_a_member_answered_is_in_no_pool_page_and_no_pool_route_response(pool_rig):
    write_pool(pool_rig)
    pool_rig.scenario(reply=MARKER)
    app = create_app(pool_rig.root)
    api = TestClient(app)
    api.headers["Authorization"] = f"Bearer {auth.mint_token(pool_rig.root, 'developer')}"
    answers = []
    asked = api.post(LEASES_PATH, json={"pool": "rebasers", "cwd": str(pool_rig.work)})
    assert asked.status_code == 200, asked.text
    answers.append(asked)
    result = asked.json()["result"]
    grant = Grant(lease_id=result["lease_id"], agent=result["agent"], token=result["token"])
    # the member says the marker to its holder, so it is there to leak
    said = prompt(grant, "say it")
    assert said.exit_code == 0, said.output
    assert MARKER in said.stdout

    pages = [page(signed_in(app, identity)) for identity in ("developer", "requester")]
    assert grant.lease_id in pages[0]
    for route in pool_routes.router.routes:
        if route.path in (LEASES_PATH, release_path("{lease_id}")):
            continue
        for method in sorted(route.methods):
            body = {} if method == "GET" else {"json": {"tokens": [grant.token]}}
            answers.append(api.request(method, route.path, **body))
    held = api.post(HELD_PATH, json={"tokens": [grant.token]})
    assert held.json()["result"][0]["lease_id"] == grant.lease_id
    released = api.post(release_path(grant.lease_id), json={"token": grant.token})
    assert released.status_code == 200, released.text
    answers += [held, released]
    pages += [page(signed_in(app, identity)) for identity in ("developer", "requester")]

    assert len(answers) >= 6
    for html in pages:
        assert MARKER not in html
    for answer in answers:
        assert MARKER not in answer.text, answer.request.url


def test_faults_of_a_reload_that_was_refused_are_shown_above_the_pool_that_still_stands(
    pool_rig, busy
):
    # A reload that does not pass keeps the last valid configuration in
    # force. The page has to say so, or the only sign is the CLI output of
    # whoever ran the reload.
    app = application(pool_rig, busy["svc"])
    fault = pool_config.ConfigError(
        cli_admin.pool_dir(pool_rig.root) / "pool.yaml",
        "member 'coder-gemma'",
        "nonsense_key",
        "unknown key; expected one of name, kind, command.",
    )
    app.state.pool_errors = (fault,)

    html = page(signed_in(app))

    assert str(fault) in unescape(html)
    # the pool that still stands is shown as well
    assert '<section id="pools">' in html


def test_a_page_with_no_faults_carries_no_fault_section(pool_rig, busy):
    html = page(signed_in(application(pool_rig, busy["svc"])))

    assert '<section id="pool-errors">' not in html
    assert '<section id="pools">' in html
