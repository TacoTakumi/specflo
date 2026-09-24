"""When the user manager cannot be reached, a member starts under the per-uid limit.

No scope can be made without the user manager: at a login session's end with
no linger, or for a daemon started outside any session. A member then starts
as it did before scopes, under prlimit's per-uid process count, with a larger
margin above the uid's threads at start: that count is shared with the host
and every other member. The daemon says why in its log, once, and not at every
member's start.
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


def test_a_member_starts_without_the_scope_under_the_uid_limit_and_the_larger_margin(
    tmp_path, environ, monkeypatch
):
    monkeypatch.setattr(sandbox, "uid_threads", lambda uid=None: 3000)

    argv = command(tmp_path, environ)

    names = [Path(word).name for word in argv]
    assert "systemd-run" not in names
    assert names[0] == "prlimit"
    (nproc,) = [word for word in argv if word.startswith("--nproc=")]
    expected = 3000 + sandbox.FALLBACK_MARGIN + sandbox._SANDBOX_PROCESSES
    _, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    if hard != resource.RLIM_INFINITY:
        expected = min(expected, hard)
    assert nproc == f"--nproc={expected}"
    assert sandbox.FALLBACK_MARGIN > sandbox.DEFAULT_LIMITS.max_procs


def test_the_reason_is_logged_once_per_daemon_not_once_per_member(tmp_path, environ, caplog):
    with caplog.at_level(logging.WARNING, logger=sandbox.__name__):
        command(tmp_path, environ, "hosted-1")
        command(tmp_path, environ, "hosted-2")
        command(tmp_path, environ, "hosted-1")

    told = [r.getMessage() for r in caplog.records if r.name == sandbox.__name__]
    assert len(told) == 1, told
    assert "user manager" in told[0]
    assert sandbox.RUNTIME_DIR_VAR in told[0]


def test_a_failed_probe_is_the_reason_the_log_gives(tmp_path, environ, caplog, monkeypatch):
    environ = {**environ, sandbox.RUNTIME_DIR_VAR: str(tmp_path / "no-runtime")}

    with caplog.at_level(logging.WARNING, logger=sandbox.__name__):
        argv = command(tmp_path, environ)

    assert "systemd-run" not in [Path(word).name for word in argv]
    (told,) = [r.getMessage() for r in caplog.records if r.name == sandbox.__name__]
    assert "Failed to connect to bus" in told
