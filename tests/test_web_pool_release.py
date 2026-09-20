"""The release control of the pool page: the developer ends a lease from the browser.

The developer's page carries one control on each active lease, a small form
that echoes the session secret; the requester's page carries none, and nothing
but a lease has one. Submitting it ends the lease through the service's one
function that ends a lease, with the cause 'released by developer': the
member's process stops, its slot is free to the next request, and the audit
log names the developer and the lease. The control on a member lease of a
team releases the whole team, and the audit names the team lease.

A post that does not echo the session secret, and one from the requester's
session, are refused and the lease stays active. A lease that ended in between
is left as it ended, an id that names no lease is a 404 page, a member that
does not stop is the log's matter, and with the pool off the post is refused.
"""

from __future__ import annotations

import logging
import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import pool_web, web
from specflo.daemon.app import create_app
from specflo.pool import service as pool_service
from specflo.pool.runner import RunnerError

# The pool tests' rig and the pool page's helpers, imported rather than copied.
# The fixtures are named here so that pytest finds them from this module.
from pool.conftest import no_real_llama_swap, no_real_provider, pool_rig  # noqa: F401  (fixtures)
from pool.test_cli_validate import _write_three_faults
from pool.test_lease_request import audit_records
from pool.test_preempt import stamps  # noqa: F401  (fixture)
from pool.test_runner import pid_alive, wait_until
from pool.test_team_lease import team_config
from test_web_pool import MARKER, POOL_PAGE, application, page, rows, section, signed_in
from test_web_pool import busy  # noqa: F401  (fixture)

CAUSE = "released by developer"
SECTIONS = ("pools", "waiting", "members", "leases", "accounts", "standing", "transitions")


def controls(html: str, name: str = "leases") -> dict[str, tuple[str, dict[str, str]]]:
    """The release forms of the section *name*, by the lease of the row each
    stands in: the URL it posts to and the hidden fields it carries."""
    found = {}
    for lease_id, body in re.findall(r'<tr data-lease="([^"]+)">(.*?)</tr>', section(html, name),
                                     re.S):
        for tag, inner in re.findall(r"(<form[^>]*>)(.*?)</form>", body, re.S):
            assert 'method="post"' in tag
            assert "<button" in inner
            fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">',
                                     inner))
            action = unescape(re.search(r'action="([^"]*)"', tag).group(1))
            assert lease_id not in found, "one control on a lease"
            found[unescape(lease_id)] = (action, fields)
    return found


def release(client, lease_id: str):
    """Submit the control the page shows on *lease_id*, as a browser does."""
    action, fields = controls(page(client))[lease_id]
    return client.post(action, data=fields)


def lease_state(pool_rig, lease_id: str) -> str:
    with pool_rig.store() as store:
        return store.get_lease(lease_id).state


def causes(pool_rig, lease_id: str) -> list[tuple[str, str]]:
    with pool_rig.store() as store:
        return [(t.kind, t.cause) for t in store.list_transitions(lease_id=lease_id)]


@pytest.fixture
def leased(pool_rig):
    """One lease out of the pool "rebasers", on a member with a process of its own."""
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    yield {"svc": svc, "grant": grant, "app": application(pool_rig, svc)}
    svc.end_lease(grant.lease_id, "released")


# -- who is shown the control, and on what ------------------------------------------


def test_the_developers_page_shows_one_control_on_each_active_lease_and_on_nothing_else(
    pool_rig, busy
):
    client = signed_in(application(pool_rig, busy["svc"]))

    html = page(client)

    shown = controls(html)
    assert list(shown) == list(rows(html, "leases", "lease")) == [busy["granted"].lease_id]
    (_, fields), = shown.values()
    assert fields == {"session": client.cookies[web.SESSION_COOKIE]}
    # the standing entries, the waiting requests and the ended leases have none
    for name in SECTIONS:
        if name != "leases":
            assert "<form" not in section(html, name), name
            assert "<button" not in section(html, name), name
    assert html.count("<form") == 1


def test_the_requesters_page_shows_no_control(pool_rig, busy):
    client = signed_in(application(pool_rig, busy["svc"]), "requester")

    html = page(client)

    assert list(rows(html, "leases", "lease")) == [busy["granted"].lease_id]
    assert "<form" not in html and "<button" not in html
    assert client.cookies[web.SESSION_COOKIE] not in html


# -- the release ----------------------------------------------------------------------


def test_submitting_the_control_ends_the_lease_stops_the_member_and_frees_its_slot(
    pool_rig, leased
):
    svc, grant = leased["svc"], leased["grant"]
    status = pool_rig.status(grant.agent)
    assert pid_alive(status["pi_pid"])
    client = signed_in(leased["app"])

    response = release(client, grant.lease_id)

    assert response.status_code == 303, response.text
    assert response.headers["location"] == POOL_PAGE
    assert lease_state(pool_rig, grant.lease_id) == "released"
    assert causes(pool_rig, grant.lease_id)[-1] == ("released", CAUSE)
    assert wait_until(lambda: not pid_alive(status["pi_pid"]))
    assert wait_until(lambda: not pid_alive(status["host_pid"]))
    # the page it comes back to shows the lease out no more, and who ended it
    html = page(client)
    assert rows(html, "leases", "lease") == {}
    newest = list(rows(html, "transitions", "transition").values())[0]
    assert newest[1:] == ["released", grant.lease_id, CAUSE]
    # the slot is free to the next request
    successor = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    assert successor.agent == grant.agent
    svc.end_lease(successor.lease_id, "released")


def test_the_release_is_audited_with_the_developer_and_the_lease(pool_rig, leased):
    grant = leased["grant"]

    release(signed_in(leased["app"]), grant.lease_id)

    (record,) = audit_records(pool_rig.root)
    assert record["identity"] == "developer"
    assert record["operation"] == "lease_release"
    assert record["id"] == grant.lease_id
    assert record["project"] is None


def test_the_control_on_a_member_lease_of_a_team_releases_the_whole_team(pool_rig):
    svc = pool_rig.service(team_config(pool_rig))
    team = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)
    statuses = [pool_rig.status(member.agent) for member in team.members]
    client = signed_in(application(pool_rig, svc))
    assert list(controls(page(client))) == [member.lease_id for member in team.members]

    # the control of the second member, and the form names no other lease
    response = release(client, team.members[1].lease_id)

    assert response.status_code == 303, response.text
    for member, status in zip(team.members, statuses):
        assert lease_state(pool_rig, member.lease_id) == "released"
        assert causes(pool_rig, member.lease_id)[-1] == ("released", CAUSE)
        assert wait_until(lambda: not pid_alive(status["pi_pid"]))
    (record,) = audit_records(pool_rig.root)
    assert (record["identity"], record["operation"]) == ("developer", "lease_release")
    assert record["id"] == team.team_lease_id


def test_the_control_records_the_developer_for_every_member_of_a_team_one_member_has_kept(
    pool_rig, stamps
):
    """The expiry check runs before each ending, and an ended member renews no
    one: the member whose prompts kept the team is ended last, so no other
    member is found idle without it and recorded as expired."""
    svc = pool_rig.service(team_config(pool_rig))  # every pool's idle limit is 10 minutes
    team = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)
    worker = team.members[0]
    for _ in range(3):  # the worker is prompted at minutes 9, 18 and 27, the critic never
        pool_rig.clock.advance(minutes=9)
        stamps[worker.agent] = {
            "state": "idle", "last_activity": pool_rig.clock().isoformat(timespec="milliseconds"),
        }
    pool_rig.clock.advance(minutes=3)
    client = signed_in(application(pool_rig, svc))

    response = release(client, worker.lease_id)

    assert response.status_code == 303, response.text
    for member in team.members:
        assert causes(pool_rig, member.lease_id)[-1] == ("released", CAUSE)
    (record,) = audit_records(pool_rig.root)
    assert (record["identity"], record["id"]) == ("developer", team.team_lease_id)


# -- refusals: the lease stays active --------------------------------------------------


@pytest.mark.parametrize("fields", [{}, {"session": "wrong"}, {"session": "caf\u00e9"}])
def test_a_post_that_does_not_echo_the_session_secret_is_refused(pool_rig, leased, fields):
    grant = leased["grant"]
    client = signed_in(leased["app"])
    action, _ = controls(page(client))[grant.lease_id]

    response = client.post(action, data=fields)

    assert response.status_code == 403
    assert "did not come from this session" in response.text
    assert lease_state(pool_rig, grant.lease_id) == "active"
    assert pid_alive(pool_rig.status(grant.agent)["pi_pid"])
    assert audit_records(pool_rig.root) == []


def test_a_post_from_the_requesters_session_is_refused(pool_rig, leased):
    grant = leased["grant"]
    action, _ = controls(page(signed_in(leased["app"])))[grant.lease_id]
    requester = signed_in(leased["app"], "requester")

    # with the secret of its own session, so only who it is refuses it
    response = requester.post(
        action, data={"session": requester.cookies[web.SESSION_COOKIE]}
    )

    assert response.status_code == 403
    assert "developer" in response.text
    assert lease_state(pool_rig, grant.lease_id) == "active"
    assert pid_alive(pool_rig.status(grant.agent)["pi_pid"])
    assert audit_records(pool_rig.root) == []


def test_the_session_secret_is_checked_before_who_posts(pool_rig, leased):
    action, _ = controls(page(signed_in(leased["app"])))[leased["grant"].lease_id]
    requester = signed_in(leased["app"], "requester")

    response = requester.post(action, data={"session": "wrong"})

    assert response.status_code == 403
    assert "did not come from this session" in response.text


def test_a_browser_with_no_session_is_sent_to_sign_in(pool_rig, leased):
    grant = leased["grant"]
    action, fields = controls(page(signed_in(leased["app"])))[grant.lease_id]
    response = TestClient(leased["app"], follow_redirects=False).post(action, data=fields)

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH
    assert lease_state(pool_rig, grant.lease_id) == "active"


# -- a lease that is not there to release -------------------------------------------------


def test_a_lease_its_holder_released_in_between_is_left_as_it_ended(pool_rig, leased):
    svc, grant = leased["svc"], leased["grant"]
    client = signed_in(leased["app"])
    action, fields = controls(page(client))[grant.lease_id]
    svc.end_lease(grant.lease_id, "released")
    before = causes(pool_rig, grant.lease_id)

    response = client.post(action, data=fields)

    assert response.status_code == 303, response.text
    assert response.headers["location"] == POOL_PAGE
    assert causes(pool_rig, grant.lease_id) == before
    assert before[-1] == ("released", "released")
    assert audit_records(pool_rig.root) == []


def test_a_lease_that_expired_in_between_is_left_as_it_ended(pool_rig, leased, stamps):
    # the host's stamp is told by the test, so the rig's clock alone judges the lease
    grant = leased["grant"]
    client = signed_in(leased["app"])
    action, fields = controls(page(client))[grant.lease_id]
    pool_rig.clock.advance(minutes=11)

    response = client.post(action, data=fields)

    assert response.status_code == 303, response.text
    assert causes(pool_rig, grant.lease_id)[-1][0] == "expired"
    assert audit_records(pool_rig.root) == []


def test_an_ending_that_won_the_race_inside_the_service_is_not_audited(
    pool_rig, leased, monkeypatch
):
    svc, grant = leased["svc"], leased["grant"]
    client = signed_in(leased["app"])
    action, fields = controls(page(client))[grant.lease_id]
    end_lease = svc.end_lease
    # the holder's own release comes first, after the route has read the lease
    # as active: the route's call is told how the lease ended, as the service does
    monkeypatch.setattr(
        svc, "end_lease", lambda lease_id, kind, **keys: end_lease(lease_id, "released")
    )

    response = client.post(action, data=fields)

    assert response.status_code == 303, response.text
    assert causes(pool_rig, grant.lease_id)[-1] == ("released", "released")
    assert audit_records(pool_rig.root) == []


def test_an_id_that_names_no_lease_is_a_404_page(pool_rig, leased):
    client = signed_in(leased["app"])
    action, fields = controls(page(client))[leased["grant"].lease_id]

    response = client.post(action.replace(leased["grant"].lease_id, "lease-404"), data=fields)

    assert response.status_code == 404
    assert "lease-404" in unescape(response.text)
    assert "<h1>" in response.text
    assert lease_state(pool_rig, leased["grant"].lease_id) == "active"
    assert audit_records(pool_rig.root) == []


# -- a member that does not stop ------------------------------------------------------------


def test_a_member_that_does_not_stop_is_the_logs_matter_and_the_release_is_audited(
    pool_rig, leased, monkeypatch, caplog
):
    grant = leased["grant"]
    client = signed_in(leased["app"])
    action, fields = controls(page(client))[grant.lease_id]
    stop = pool_service.runner.stop

    def stuck(agent, *args, **keys):
        stop(agent, *args, **keys)
        raise RunnerError(f"member '{agent}' did not stop: /home/someone/pi: {MARKER}")

    monkeypatch.setattr(pool_service.runner, "stop", stuck)

    with caplog.at_level(logging.WARNING, logger=pool_web.__name__):
        response = client.post(action, data=fields)

    assert response.status_code == 303, response.text
    assert response.headers["location"] == POOL_PAGE
    assert lease_state(pool_rig, grant.lease_id) == "released"
    assert causes(pool_rig, grant.lease_id)[-1] == ("released", CAUSE)
    (record,) = audit_records(pool_rig.root)
    assert (record["identity"], record["id"]) == ("developer", grant.lease_id)
    assert MARKER in caplog.text
    # what the runner said can hold host paths and member output: the log's alone
    assert MARKER not in response.text
    html = page(client)
    assert MARKER not in html and "/home/someone" not in html


# -- the pool is off ----------------------------------------------------------------------------


def _no_pool(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


def _faulty_pool(tmp_path):
    return _write_three_faults(tmp_path / "daemon")


@pytest.mark.parametrize("root_of", [_no_pool, _faulty_pool])
def test_with_the_pool_off_the_post_is_refused_plainly(tmp_path, root_of):
    client = signed_in(create_app(root_of(tmp_path)))
    action = pool_web.release_url("lease-1")

    response = client.post(action, data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 409
    assert "pool" in response.text.lower()
    assert "Traceback" not in response.text
