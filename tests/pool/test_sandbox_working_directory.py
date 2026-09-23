"""The working directories a member is refused, because its bind would undo the sandbox.

A member's working directory is bound in writable after the directories that
lie above a bind are hidden. So a working directory at or above a hidden
path brings all of it back, writable: with the home as the working
directory, a member reads ``~/.ssh`` and writes the ``~/.bashrc`` the
operator's next shell runs. A working directory inside a hidden path other
than the home brings back that part of it. Both are refused, by the one
builder every start goes through and by the route a request arrives at.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from specflo.daemon.app import create_app
from specflo.service.pool_remote import LEASES_PATH
from specflo.pool import launch
from specflo.pool.config import Member

from .test_lease_request import pool_daemon  # noqa: F401 - a fixture
from .test_runner import DEFINITION

MEMBER = Member(
    name="hosted-1", command="python", backing="hosted", labels=(), capacity=1,
    egress="no-train", model="some-vendor/some-model", account="team-a",
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "rig" / "home"
    for name in (".pi/agent", ".ssh", "work/project"):
        (home / name).mkdir(parents=True)
    return home


@pytest.fixture
def environ(home: Path) -> dict[str, str]:
    runtime = home.parent / "runtime"
    runtime.mkdir()
    return {"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime), "PATH": os.environ["PATH"]}


@pytest.fixture
def daemon_root(tmp_path: Path) -> Path:
    root = tmp_path / "srv" / "specflo"
    (root / "pi-config" / "other-abc").mkdir(parents=True)
    return root


def build(tmp_path: Path, environ, cwd: Path, daemon_root: Path) -> list[str]:
    return launch.member_argv(
        DEFINITION, MEMBER, environ, cwd=cwd, state_dir=tmp_path / "state",
        daemon_root=daemon_root,
    )


def refused(tmp_path, environ, daemon_root, home):
    """Every working directory the sandbox would come undone in."""
    link = tmp_path / "to-home"
    link.symlink_to(home)
    inside = home / "work" / "project" / ".specflo" / "leases"
    inside.mkdir(parents=True)
    return {
        "the root": Path("/"),
        "the home": home,
        "above the home": home.parent,
        "a link to the home": link,
        "a hidden directory": home / ".pi",
        "inside a hidden directory": home / ".pi" / "agent",
        "the daemon root": daemon_root,
        "above the daemon root": daemon_root.parent,
        "inside the daemon root": daemon_root / "pi-config" / "other-abc",
        "inside a checkout's tokens": inside,
    }


def test_a_working_directory_that_would_bring_back_a_hidden_path_is_refused(
    tmp_path, environ, daemon_root, home
) -> None:
    for why, cwd in refused(tmp_path, environ, daemon_root, home).items():
        with pytest.raises(launch.LaunchError) as caught:
            build(tmp_path, environ, cwd, daemon_root)
        assert "working directory" in str(caught.value), why
        assert str(cwd) in str(caught.value), why


def test_a_working_directory_under_the_home_that_holds_no_hidden_path_starts(
    tmp_path, environ, daemon_root, home
) -> None:
    cwd = home / "work" / "project"

    argv = build(tmp_path, environ, cwd, daemon_root)

    assert ["--chdir", str(cwd)] == argv[argv.index("--chdir"):argv.index("--chdir") + 2]


def test_the_working_directory_check_says_why_and_nothing_when_it_is_fine(
    environ, daemon_root, home
) -> None:
    assert launch.working_directory_fault(home / "work", environ, daemon_root) is None
    said = launch.working_directory_fault(home, environ, daemon_root)
    assert said is not None and "home" in said


def test_the_route_refuses_a_working_directory_at_the_home_before_anything_starts(
    pool_daemon, pool_rig  # noqa: F811 - the fixture above
) -> None:
    client = TestClient(create_app(pool_daemon["root"]))
    client.headers["Authorization"] = f"Bearer {pool_daemon['token']}"
    home = os.environ.get("HOME") or str(Path.home())

    for cwd in (home, "/", str(pool_daemon["root"])):
        response = client.post(LEASES_PATH, json={"pool": "rebasers", "cwd": cwd})
        assert response.status_code == 400, (cwd, response.text)
        assert "working directory" in response.json()["detail"]
    assert pool_rig.pane_names() == []
    with pool_rig.store() as store:
        assert store.list_leases() == []
