"""A release whose cleanup fails still ends the lease and answers success.

The generated directory is removed once the member has stopped. That removal
can fail, and when it does the lease has ended all the same: the member is
gone, the ledger says so, the release is audited, and the holder is told it
succeeded. The failure is logged, and the daemon's record of the directory
goes, so the next start of the member does not write over a record that still
names it; the orphan sweep takes what the failed removal left.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from specflo.agent.statefiles import AgentPaths
from specflo.daemon import auth
from specflo.pool import runner
from specflo.service.pool_remote import release_path

from .conftest import no_real_llama_swap, no_real_provider, pool_rig  # noqa: F401  (fixtures)
from .test_lease_request import audit_records
from .test_team_lease import team_config


@pytest.fixture
def failing_removal(monkeypatch):
    def refuse(directory, root):
        raise OSError(13, "Permission denied", str(directory))

    monkeypatch.setattr(runner.piconfig, "remove", refuse)


def api(app) -> TestClient:
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {auth.mint_token(app.state.root, 'developer')}"
    return client


def state(pool_rig, lease_id: str) -> str:
    with pool_rig.store() as store:
        return store.get_lease(lease_id).state


def records_left(agent: str) -> list[str]:
    root = AgentPaths.resolve(agent).root
    return [f for f in (runner.CONFIG_DIR_FILE, runner.CONFIG_ROOT_FILE) if (root / f).exists()]


def test_a_release_through_the_api_ends_the_lease_and_answers_success(
    pool_rig, failing_removal, caplog
):
    from test_web_pool import application

    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with caplog.at_level(logging.WARNING, logger=runner.__name__):
        answer = api(application(pool_rig, svc)).post(
            release_path(grant.lease_id), json={"token": grant.token}
        )

    assert answer.status_code == 200, answer.text
    assert answer.json()["result"]["state"] == "released"
    assert state(pool_rig, grant.lease_id) == "released"
    assert audit_records(pool_rig.root)[-1]["operation"] == "lease_release"
    assert "Permission denied" in caplog.text
    assert records_left(grant.agent) == []


def test_a_team_release_ends_every_member_and_answers_success(pool_rig, failing_removal, caplog):
    from test_web_pool import application

    svc = pool_rig.service(team_config(pool_rig))
    team = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)

    with caplog.at_level(logging.WARNING, logger=runner.__name__):
        answer = api(application(pool_rig, svc)).post(
            release_path(team.team_lease_id), json={"token": team.members[0].token}
        )

    assert answer.status_code == 200, answer.text
    assert [state(pool_rig, m.lease_id) for m in team.members] == ["released"] * len(team.members)
    assert audit_records(pool_rig.root)[-1]["id"] == team.team_lease_id
    assert "Permission denied" in caplog.text
    assert [records_left(m.agent) for m in team.members] == [[]] * len(team.members)


def test_a_release_from_the_pool_page_ends_the_lease_and_answers_success(
    pool_rig, failing_removal, caplog
):
    from test_web_pool import application, signed_in
    from test_web_pool_release import release

    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    client = signed_in(application(pool_rig, svc))

    with caplog.at_level(logging.WARNING, logger=runner.__name__):
        answer = release(client, grant.lease_id)

    assert answer.status_code == 303, answer.text
    assert state(pool_rig, grant.lease_id) == "released"
    assert audit_records(pool_rig.root)[-1]["operation"] == "lease_release"
    assert "Permission denied" in caplog.text
    assert records_left(grant.agent) == []
