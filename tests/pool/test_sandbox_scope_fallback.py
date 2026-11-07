"""When the user manager cannot be reached, a member's process limit is set in its sandbox.

No scope can be made without the user manager: at a login session's end with
no linger, or for a daemon started outside any session. A member's process
limit is then a prlimit after bwrap's separator, inside the user namespace,
where the kernel counts the sandbox's own tasks. On a kernel that counts it
per uid across the host there, a member starts as it did before scopes, under
prlimit's per-uid process count in front of bwrap, with a larger margin above
the uid's threads at start: that count is shared with the host and every other
member. The daemon says why in its log, once, and not at every member's start.
"""

from __future__ import annotations

import logging
import os
import resource
import sys
from pathlib import Path

import pytest

from specflo.pool import launch, sandbox
from specflo.pool.config import Member

from .test_runner import DEFINITION


@pytest.fixture(autouse=True)
def untold(monkeypatch):
    """Each test is a daemon that has said nothing of the manager yet."""
    monkeypatch.setattr(sandbox, "_told_no_scope", False)
    monkeypatch.setattr(sandbox, "_nproc_scoped", None)


@pytest.fixture
def environ(tmp_path) -> dict[str, str]:
    """The daemon's environment with no way to the user manager."""
    home = tmp_path / "home"
    home.mkdir()
    return {"HOME": str(home), "PATH": os.environ["PATH"]}


def command(tmp_path: Path, environ, name: str = "hosted-1") -> list[str]:
    work = tmp_path / name
    work.mkdir(exist_ok=True)
    member = Member(
        name=name, backing="hosted", labels=(), capacity=1, egress="no-train",
        model="some-vendor/some-model", account="team-a", command=sys.executable,
    )
    return launch.member_argv(DEFINITION, member, environ, cwd=work, state_dir=tmp_path / "state")


def clamped(value: int) -> int:
    _, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    return value if hard == resource.RLIM_INFINITY else min(value, hard)


def test_a_member_without_the_scope_gets_its_process_limit_inside_the_sandbox(
    tmp_path, environ, monkeypatch
):
    monkeypatch.setattr(sandbox, "_nproc_scoped", True)
    monkeypatch.setattr(sandbox, "uid_threads", lambda uid=None: 3000)

    argv = command(tmp_path, environ)

    names = [Path(word).name for word in argv]
    assert "systemd-run" not in names
    separator = argv.index("--")
    assert names.index("bwrap") < separator
    assert not [word for word in argv[:separator] if word.startswith("--nproc=")]
    assert names[separator + 1] == "prlimit"
    expected = clamped(sandbox.DEFAULT_LIMITS.max_procs + sandbox._SANDBOX_INIT)
    assert argv[separator + 2] == f"--nproc={expected}"


def test_a_kernel_that_counts_per_uid_keeps_the_uid_limit_and_the_larger_margin(
    tmp_path, environ, monkeypatch
):
    monkeypatch.setattr(sandbox, "_nproc_scoped", False)
    monkeypatch.setattr(sandbox, "uid_threads", lambda uid=None: 3000)

    argv = command(tmp_path, environ)

    names = [Path(word).name for word in argv]
    assert "systemd-run" not in names
    assert names[0] == "prlimit"
    (nproc,) = [word for word in argv if word.startswith("--nproc=")]
    expected = clamped(3000 + sandbox.FALLBACK_MARGIN + sandbox._SANDBOX_PROCESSES)
    assert nproc == f"--nproc={expected}"
    assert argv.index(nproc) < [Path(word).name for word in argv].index("bwrap")
    assert sandbox.FALLBACK_MARGIN > sandbox.DEFAULT_LIMITS.max_procs


def test_the_reason_is_logged_once_per_daemon_not_once_per_member(
    tmp_path, environ, caplog, monkeypatch
):
    monkeypatch.setattr(sandbox, "_nproc_scoped", True)
    with caplog.at_level(logging.WARNING, logger=sandbox.__name__):
        command(tmp_path, environ, "hosted-1")
        command(tmp_path, environ, "hosted-2")
        command(tmp_path, environ, "hosted-1")

    told = [r.getMessage() for r in caplog.records if r.name == sandbox.__name__]
    assert len(told) == 1, told
    assert "user manager" in told[0]
    assert sandbox.RUNTIME_DIR_VAR in told[0]


def test_a_failed_probe_is_the_reason_the_log_gives(tmp_path, environ, caplog, monkeypatch):
    monkeypatch.setattr(sandbox, "_nproc_scoped", True)
    environ = {**environ, sandbox.RUNTIME_DIR_VAR: str(tmp_path / "no-runtime")}

    with caplog.at_level(logging.WARNING, logger=sandbox.__name__):
        argv = command(tmp_path, environ)

    assert "systemd-run" not in [Path(word).name for word in argv]
    (told,) = [r.getMessage() for r in caplog.records if r.name == sandbox.__name__]
    assert "Failed to connect to bus" in told


def test_a_kernel_that_counts_per_uid_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(sandbox, "prlimit_path", lambda: "/nonexistent/prlimit")

    with caplog.at_level(logging.WARNING, logger=sandbox.__name__):
        assert sandbox.nproc_scoped() is False
        assert sandbox.nproc_scoped() is False

    told = [r.getMessage() for r in caplog.records if r.name == sandbox.__name__]
    assert len(told) == 1, told
    assert "per-uid" in told[0]
