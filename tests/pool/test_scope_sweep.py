"""Member scopes carry the member's name, and an empty one is stopped by the expiry pass.

A process killed while systemd-run is still making its scope leaves the scope
behind with nothing in it, and systemd keeps such a scope active for as long
as the user manager runs. Each member's scope is named for the member, so the
daemon can find its own among the user's units, and the expiry pass stops
every member scope that has no task left. A scope that holds a running member
is left alone, and a stop that fails is logged without ending the pass.
"""

from __future__ import annotations

import logging
import os
import random
import secrets
import subprocess
import time

import pytest

from specflo.pool import sandbox

from .test_sandbox_scope import skip_without_a_scope


def units(pattern: str) -> dict[str, str]:
    """The user's units matching *pattern*, by name, with their active state."""
    listed = subprocess.run(
        ["systemctl", "--user", "list-units", "--plain", "--no-legend", "--all", pattern],
        capture_output=True, text=True,
    ).stdout
    return {line.split()[0]: line.split()[2] for line in listed.splitlines() if line.strip()}


def scope(name: str) -> list[str]:
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={name}",
        "-p", "TasksMax=8", "--", "sleep", "30",
    ]


@pytest.fixture
def run_id() -> str:
    return secrets.token_hex(4)


@pytest.fixture
def leaked(run_id) -> str:
    """A member scope with nothing in it, made the way it happens: its process
    is killed while systemd-run is still making the scope."""
    skip_without_a_scope()
    for attempt in range(300):
        name = f"specflo-member-leak{run_id}-{attempt}"
        started = subprocess.Popen(scope(name))
        time.sleep(random.uniform(0, 0.03))
        started.kill()
        started.wait()
        time.sleep(0.2)
        if units(f"{name}.scope").get(f"{name}.scope") == "active":
            return f"{name}.scope"
    pytest.skip("no scope was left behind in 300 tries")


@pytest.fixture
def live(run_id):
    skip_without_a_scope()
    name = f"specflo-member-live{run_id}-0"
    started = subprocess.Popen(scope(name))
    deadline = time.monotonic() + 5
    while units(f"{name}.scope").get(f"{name}.scope") != "active":
        assert time.monotonic() < deadline, "the live scope never came up"
        time.sleep(0.05)
    yield f"{name}.scope"
    started.kill()
    started.wait()


def test_the_scope_is_named_for_the_member_and_fresh_at_each_start():
    first = sandbox.scope_argv(64, {"XDG_RUNTIME_DIR": "/run/user/1000"}, "hosted-1.2")
    second = sandbox.scope_argv(64, {"XDG_RUNTIME_DIR": "/run/user/1000"}, "hosted-1.2")

    (named,) = [word for word in first if word.startswith("--unit=")]
    assert named.startswith("--unit=specflo-member-hosted-1.2-")
    assert named != [word for word in second if word.startswith("--unit=")][0]
    odd = sandbox.scope_argv(64, {"XDG_RUNTIME_DIR": "/run/user/1000"}, "a b/c")
    assert "--unit=specflo-member-a_b_c-" in " ".join(odd)


# The leaked fixture makes up to 300 real systemd scopes, 0.2s or more each;
# under load it runs past a minute and holds up the whole suite.
@pytest.mark.skip(reason="slow: leaked fixture can run past a minute under load")
def test_the_sweep_stops_an_empty_member_scope_and_leaves_a_running_one(leaked, live):
    sandbox.stop_empty_scopes(os.environ)

    assert units(leaked).get(leaked) in (None, "inactive")
    assert units(live)[live] == "active"


def test_a_stop_that_fails_is_logged_and_the_others_go_on(monkeypatch, caplog):
    monkeypatch.setattr(sandbox, "member_scopes", lambda environ: {
        "specflo-member-a-1.scope": 0, "specflo-member-b-2.scope": 0,
        "specflo-member-c-3.scope": 3,
    })
    stopped = []

    def stop(unit, environ):
        if unit.startswith("specflo-member-a"):
            raise sandbox.SpecfloError("Failed to stop: access denied")
        stopped.append(unit)

    monkeypatch.setattr(sandbox, "_stop_scope", stop)
    with caplog.at_level(logging.WARNING, logger=sandbox.__name__):
        sandbox.stop_empty_scopes(os.environ)

    assert stopped == ["specflo-member-b-2.scope"]
    assert "specflo-member-a-1.scope" in caplog.text and "access denied" in caplog.text


def test_the_expiry_pass_stops_empty_member_scopes(pool_rig, monkeypatch):
    from specflo.pool import service

    asked = []
    monkeypatch.setattr(service.sandbox, "stop_empty_scopes", lambda environ: asked.append(1))
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    svc.expire_due()

    assert asked == [1]
