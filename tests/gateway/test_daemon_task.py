"""The gateway runs as a task of the daemon, and only when the root configures it.

With a valid ``gateway.yaml`` the daemon starts one gateway task while it
serves, which polls at once and then at each interval, and cancels it when it
stops. Without the file, or with a faulty one, there is no task and the forge
is never asked: the stub poll step stands in for the forge and records each
time it would be asked. A faulty file logs each fault with the field it names,
and the daemon serves all the same.
"""

import asyncio
import dataclasses
import logging
import os
import threading

import pytest
import yaml
from fastapi.testclient import TestClient
from waits import scaled, settle

from specflo import daemon
from specflo.daemon.app import create_app
from specflo.gateway import Gateway, config, gitea, service

from .stub_forge import StubGitea

TOKEN = "fake-gitea-token-51be"


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


class StubForge:
    """A poll step that records each poll, as a forge would record each request."""

    def __init__(self):
        self.polls = []
        self.polled = threading.Event()

    async def __call__(self, gateway_config):
        self.polls.append(gateway_config)
        self.polled.set()


def _configure(root, mode=0o600, **fields):
    token = root / "gitea.token"
    token.write_text(TOKEN, encoding="utf-8")
    os.chmod(token, mode)
    forge = {
        "kind": "gitea",
        "url": "https://gitea.example",
        "repos": ["me/thing"],
        "operator_id": 7,
        "pool": "rfc-research",
        "token_file": "gitea.token",
    }
    forge.update(fields)
    forge = {key: value for key, value in forge.items() if value is not None}
    (root / config.CONFIG_FILE).write_text(
        yaml.safe_dump({"forges": [forge]}), encoding="utf-8"
    )


def _app(root, forge):
    app = create_app(root)
    app.state.gateway_poll = forge
    return app


def test_a_configured_root_runs_the_gateway_task_until_the_daemon_stops(root):
    _configure(root)
    forge = StubForge()
    app = _app(root, forge)

    with TestClient(app) as client:
        task = app.state.gateway_task
        assert task is not None
        assert forge.polled.wait(scaled(10))
        assert not task.done()
        assert client.get(daemon.HEALTH_PATH).json() == {"status": "ok"}
        assert forge.polls[0] is app.state.gateway
        assert forge.polls[0].forges[0].repos == ("me/thing",)

    assert task.cancelled()


def test_a_root_without_the_configuration_starts_no_task_and_asks_no_forge(root):
    forge = StubForge()
    app = _app(root, forge)

    with TestClient(app) as client:
        assert client.get(daemon.HEALTH_PATH).json() == {"status": "ok"}
        settle(0.3)
        assert app.state.gateway is None
        assert app.state.gateway_errors == ()
        assert app.state.gateway_task is None

    assert forge.polls == []


@pytest.mark.parametrize(
    "fields, mode, field",
    [
        ({"kind": "gitlab"}, 0o600, "kind"),
        ({"operator_id": None}, 0o600, "operator_id"),
        ({"token_file": "missing.token"}, 0o600, "token_file"),
        ({}, 0o644, "token_file"),
        ({"repos": []}, 0o600, "repos"),
    ],
)
def test_a_faulty_configuration_logs_the_field_starts_no_task_and_the_daemon_serves(
    root, caplog, fields, mode, field
):
    _configure(root, mode=mode, **fields)
    forge = StubForge()

    with caplog.at_level(logging.WARNING):
        app = _app(root, forge)
        with TestClient(app) as client:
            assert client.get(daemon.HEALTH_PATH).json() == {"status": "ok"}
            settle(0.3)
            assert app.state.gateway is None
            assert app.state.gateway_task is None

    assert forge.polls == []
    assert [error.field for error in app.state.gateway_errors] == [field]
    faults = [r.getMessage() for r in caplog.records if "gateway configuration" in r.getMessage()]
    assert faults and all(f": {field}: " in fault for fault in faults)
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


def _run(coroutine):
    return asyncio.run(coroutine)


def test_the_loop_polls_then_waits_the_interval_and_goes_on_after_a_failed_poll(caplog):
    gateway_config = config.GatewayConfig(path=None, forges=(), poll_every=42)
    polls = []
    waits = []

    async def poll(given):
        polls.append(given)
        if len(polls) == 1:
            raise RuntimeError("the forge did not answer")

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise asyncio.CancelledError

    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        _run(Gateway(gateway_config, poll, sleep=sleep).run())

    assert polls == [gateway_config] * 3
    assert waits == [42, 42, 42]
    assert any("the forge did not answer" in r.getMessage() or r.exc_info for r in caplog.records)


def test_cancelling_the_loop_while_it_polls_ends_it_at_once():
    gateway_config = config.GatewayConfig(path=None, forges=(), poll_every=60)

    async def scenario():
        started = asyncio.Event()

        async def poll(given):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(Gateway(gateway_config, poll).run())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task

    assert _run(scenario()).cancelled()


def test_the_daemon_own_poll_step_keeps_the_task_running(root):
    _configure(root)
    app = create_app(root)
    # The daemon's own poll step, the service's, with its Gitea client on a
    # stub forge so that no request leaves the process.
    stub = StubGitea(token=TOKEN)
    stub.repos.add("me/thing")
    gateway_service = app.state.gateway_service
    assert app.state.gateway_poll == gateway_service.poll
    gateway_service.kinds[config.GITEA] = dataclasses.replace(
        service.GITEA_KIND,
        client=lambda forge: gitea.GiteaClient(forge, transport=stub.transport()),
    )

    with TestClient(app):
        task = app.state.gateway_task
        assert task is not None
        settle(0.2)
        assert not task.done()

    assert task.cancelled()
    assert stub.requests, "the daemon's poll step asked the stub forge nothing"
    assert all(seen.path.startswith("/api/v1/repos/me/thing/") for seen in stub.requests)
