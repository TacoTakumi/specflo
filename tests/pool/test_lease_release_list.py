"""``specflo lease release`` and ``specflo lease list``: a lease is its holder's.

The holder of a lease is whoever has its token. A release is granted to the
one that presents it, and a listing answers only for the tokens a client
presents, so an orchestrator sees, and gives back, its own leases and no one
else's. A lease that has ended is no one's any more: a release of it changes
nothing and reports how it ended. What a pool has out is no secret; who holds
it is, so the pool status names no holder.

The daemon and the orchestrator's checkout are those of the request verb's
tests: a real daemon on a loopback port, and members that are real processes.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from specflo import config
from specflo.cli import app
from specflo.daemon import auth
from specflo.daemon.app import create_app
from specflo.service.pool_remote import HELD_PATH, LEASES_PATH, STATUS_PATH, RemotePool

from .test_lease_request import (  # noqa: F401  (fixtures)
    audit_records,
    checkout,
    pool_daemon,
    request,
    runner,
)
from .test_runner import pid_alive


def token_file(checkout_dir, agent: str = "local-1"):
    return checkout_dir / ".specflo" / "leases" / f"{agent}.token"


def other_checkout(tmp_path, monkeypatch, pool_daemon, identity: str = "developer"):
    """A second orchestrator's checkout on the same daemon, and the shell in it."""
    other = tmp_path / "other"
    other.mkdir()
    config.init_config(other)
    monkeypatch.chdir(other)
    token = auth.mint_token(pool_daemon["root"], identity)
    registered = runner.invoke(app, ["remote", "add", "home", pool_daemon["url"], "--token", token])
    assert registered.exit_code == 0, registered.output
    return other


def listed() -> list[dict]:
    result = runner.invoke(app, ["lease", "list", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def daemon_client(pool_daemon) -> TestClient:
    client = TestClient(create_app(pool_daemon["root"]))
    client.headers["Authorization"] = f"Bearer {pool_daemon['token']}"
    return client


# -- release ----------------------------------------------------------------


def test_the_holders_release_ends_the_lease_and_removes_the_stored_token(checkout, pool_rig):
    granted = request("rebasers")
    pi_pid = pool_rig.status("local-1")["pi_pid"]

    result = runner.invoke(app, ["lease", "release", granted["lease"]])

    assert result.exit_code == 0, result.output
    assert "released" in result.stdout
    with pool_rig.store() as store:
        assert store.get_lease(granted["lease"]).state == "released"
    assert not pid_alive(pi_pid)
    assert not token_file(checkout).exists()
    # the former holder's next verb is told why the member is gone
    late = runner.invoke(app, ["agent", "prompt", "local-1", "rebase the branch"])
    assert late.exit_code != 0
    assert "lease released" in late.output
    # the slot is free to the next request
    assert request("rebasers")["agent"] == "local-1"


def test_a_second_release_exits_zero_and_reports_released(checkout, pool_rig):
    granted = request("rebasers")
    assert runner.invoke(app, ["lease", "release", granted["lease"]]).exit_code == 0
    with pool_rig.store() as store:
        before = store.list_transitions(lease_id=granted["lease"])

    again = runner.invoke(app, ["lease", "release", granted["lease"], "--json"])

    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout) == {"lease": granted["lease"], "state": "released"}
    with pool_rig.store() as store:
        assert store.list_transitions(lease_id=granted["lease"]) == before


def test_another_orchestrators_release_is_refused_and_the_lease_stays_active(
    checkout, pool_rig, pool_daemon, tmp_path, monkeypatch
):
    granted = request("rebasers")
    other = other_checkout(tmp_path, monkeypatch, pool_daemon)

    for forged in (None, "not-the-holders-token"):
        if forged is not None:
            token_file(other).parent.mkdir(parents=True, exist_ok=True)
            token_file(other).write_text(forged + "\n", encoding="utf-8")

        refused = runner.invoke(app, ["lease", "release", granted["lease"]])

        assert refused.exit_code != 0
        assert granted["lease"] in refused.output
        with pool_rig.store() as store:
            assert store.get_lease(granted["lease"]).state == "active"
        assert pid_alive(pool_rig.status("local-1")["pi_pid"])
    # what the other orchestrator keeps is its own, and stays
    assert token_file(other).exists()
    assert token_file(checkout).exists()

    # the holder still drives the member, and gives it back
    monkeypatch.chdir(checkout)
    assert runner.invoke(app, ["agent", "prompt", "local-1", "rebase"]).exit_code == 0
    assert runner.invoke(app, ["lease", "release", granted["lease"]]).exit_code == 0


def test_a_release_of_a_lease_that_is_not_there_exits_non_zero_naming_it(checkout):
    result = runner.invoke(app, ["lease", "release", "lease-none"])

    assert result.exit_code != 0
    assert "lease-none" in result.output


def test_the_route_refuses_a_release_without_the_holders_token(checkout, pool_rig, pool_daemon):
    granted = request("rebasers")
    client = daemon_client(pool_daemon)
    path = f"{LEASES_PATH}/{granted['lease']}/release"

    for body in ({}, {"token": "not-the-holders-token"}, {"token": ""}, {"holder": "me"}):
        response = client.post(path, json=body)
        assert response.status_code in (400, 422), (body, response.text)
    assert TestClient(create_app(pool_daemon["root"])).post(path, json={}).status_code == 401

    with pool_rig.store() as store:
        assert store.get_lease(granted["lease"]).state == "active"
    runner.invoke(app, ["lease", "release", granted["lease"]])


def test_a_release_is_audited_with_the_acting_identity_and_a_refusal_is_not(
    checkout, pool_daemon, tmp_path, monkeypatch
):
    granted = request("rebasers")
    other_checkout(tmp_path, monkeypatch, pool_daemon, identity="agent")
    assert runner.invoke(app, ["lease", "release", granted["lease"]]).exit_code != 0
    assert [r["operation"] for r in audit_records(pool_daemon["root"])] == ["lease_request"]

    monkeypatch.chdir(checkout)
    token = token_file(checkout).read_text(encoding="utf-8").strip()
    assert runner.invoke(app, ["lease", "release", granted["lease"]]).exit_code == 0

    record = audit_records(pool_daemon["root"])[-1]
    assert record["identity"] == "developer"
    assert record["operation"] == "lease_release"
    assert record["id"] == granted["lease"]
    assert token not in json.dumps(record)
    # a release that changes nothing is no one's act
    assert runner.invoke(app, ["lease", "release", granted["lease"]]).exit_code == 0
    assert len(audit_records(pool_daemon["root"])) == 2


# -- list -------------------------------------------------------------------


def test_the_holders_list_names_its_lease_and_another_orchestrators_omits_it(
    checkout, pool_rig, pool_daemon, tmp_path, monkeypatch
):
    granted = request("rebasers")

    (mine,) = listed()
    assert (mine["lease"], mine["agent"], mine["pool"], mine["state"]) == (
        granted["lease"], "local-1", "rebasers", "active",
    )
    shown = runner.invoke(app, ["lease", "list"])
    assert shown.exit_code == 0, shown.output
    assert granted["lease"] in shown.stdout
    token = token_file(checkout).read_text(encoding="utf-8").strip()
    assert token not in shown.output

    other = other_checkout(tmp_path, monkeypatch, pool_daemon)
    assert listed() == []
    # a token file that holds no lease's token lists nothing
    token_file(other).parent.mkdir(parents=True)
    token_file(other).write_text("not-the-holders-token\n", encoding="utf-8")
    assert listed() == []
    assert granted["lease"] not in runner.invoke(app, ["lease", "list"]).output

    monkeypatch.chdir(checkout)
    runner.invoke(app, ["lease", "release", granted["lease"]])
    assert listed() == []


def test_the_held_route_answers_only_for_the_tokens_it_is_given(
    checkout, pool_rig, pool_daemon
):
    granted = request("rebasers")
    token = token_file(checkout).read_text(encoding="utf-8").strip()
    client = daemon_client(pool_daemon)

    none = client.post(HELD_PATH, json={"tokens": []})
    assert (none.status_code, none.json()["result"]) == (200, [])
    # one answer for each token, in the order they came
    answered = client.post(HELD_PATH, json={"tokens": ["not-one", token]})
    assert answered.status_code == 200
    stranger, held = answered.json()["result"]
    assert stranger is None
    assert held["lease_id"] == granted["lease"]
    # nothing that lets another take the holder's place goes back
    assert token not in answered.text
    assert auth.hash_token(token) not in answered.text
    for body in ({}, {"tokens": "not-a-list"}, {"tokens": [7]}, {"tokens": [token], "all": True}):
        assert client.post(HELD_PATH, json=body).status_code == 422, body
    runner.invoke(app, ["lease", "release", granted["lease"]])


# -- the pool status --------------------------------------------------------


def test_the_pool_status_gives_each_pools_size_and_leases_in_use_and_no_holder(
    checkout, pool_rig, pool_daemon
):
    client = RemotePool(pool_daemon["url"], pool_daemon["token"])
    assert client.status() == [{"name": "rebasers", "size": 1, "in_use": 0}]

    granted = request("rebasers", "--label", "orchestrator-a")

    assert client.status() == [{"name": "rebasers", "size": 1, "in_use": 1}]
    response = daemon_client(pool_daemon).get(STATUS_PATH)
    token = token_file(checkout).read_text(encoding="utf-8").strip()
    for secret in ("orchestrator-a", "developer", granted["lease"], token, auth.hash_token(token)):
        assert secret not in response.text
    assert TestClient(create_app(pool_daemon["root"])).get(STATUS_PATH).status_code == 401

    runner.invoke(app, ["lease", "release", granted["lease"]])
    assert client.status() == [{"name": "rebasers", "size": 1, "in_use": 0}]
