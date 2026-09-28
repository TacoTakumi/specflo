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
import secrets
import shutil
import subprocess

import pytest

from specflo.pool import sandbox
from waits import scaled, wait_until

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


def stop_scope(unit: str) -> None:
    """Stop *unit*, if it is still there."""
    subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=scaled(30))


@pytest.fixture
def prefix(monkeypatch) -> str:
    """A scope prefix of this test's own, which the sweep looks for instead.

    The pool service of each test that runs meanwhile sweeps every empty
    member scope of the user, and would stop this test's before its own sweep
    could. A name under this prefix is not a member scope to them.
    """
    own = f"specflo-membertest{secrets.token_hex(4)}-"
    monkeypatch.setattr(sandbox, "SCOPE_PREFIX", own)
    return own


@pytest.fixture
def leaked(prefix):
    """A member scope with nothing in it, as a member killed while systemd-run
    is still making its scope leaves one: systemd starts the scope for a
    process that has exited and is not yet reaped. The kernel moves no exited
    process into a cgroup, so the scope never holds a task, no cgroup-empty
    event comes, and systemd keeps it active after the process is reaped."""
    skip_without_a_scope()
    if shutil.which("busctl") is None:
        pytest.skip("busctl is not on PATH")
    unit = f"{prefix}leak.scope"
    exited = subprocess.Popen(["true"])
    try:
        # WNOWAIT: the process has exited, and stays unreaped
        os.waitid(os.P_PID, exited.pid, os.WEXITED | os.WNOWAIT)
        started = subprocess.run(
            ["busctl", "--user", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
             "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))",
             unit, "fail", "3", "PIDs", "au", "1", str(exited.pid), "TasksMax", "t", "8",
             "CollectMode", "s", "inactive-or-failed", "0"],
            capture_output=True, text=True, timeout=scaled(10),
        )
        assert started.returncode == 0, f"the leaked scope was not started: {started.stderr}"
        wait_until(
            lambda: units(unit).get(unit) == "active",
            timeout=5, message="the leaked scope never came up",
        )
        exited.wait()
        yield unit
    finally:
        exited.wait()
        stop_scope(unit)


@pytest.fixture
def live(prefix):
    skip_without_a_scope()
    name = f"{prefix}live"
    started = subprocess.Popen(scope(name))
    try:
        wait_until(
            lambda: units(f"{name}.scope").get(f"{name}.scope") == "active",
            timeout=5, message="the live scope never came up",
        )
        yield f"{name}.scope"
    finally:
        started.kill()
        started.wait()
        stop_scope(f"{name}.scope")


def test_the_scope_is_named_for_the_member_and_fresh_at_each_start():
    first = sandbox.scope_argv(64, {"XDG_RUNTIME_DIR": "/run/user/1000"}, "hosted-1.2")
    second = sandbox.scope_argv(64, {"XDG_RUNTIME_DIR": "/run/user/1000"}, "hosted-1.2")

    (named,) = [word for word in first if word.startswith("--unit=")]
    assert named.startswith("--unit=specflo-member-hosted-1.2-")
    assert named != [word for word in second if word.startswith("--unit=")][0]
    odd = sandbox.scope_argv(64, {"XDG_RUNTIME_DIR": "/run/user/1000"}, "a b/c")
    assert "--unit=specflo-member-a_b_c-" in " ".join(odd)


def test_the_sweep_stops_an_empty_member_scope_and_leaves_a_running_one(leaked, live):
    found = sandbox.member_scopes(os.environ)
    assert found[leaked] == 0 and found[live] > 0

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
