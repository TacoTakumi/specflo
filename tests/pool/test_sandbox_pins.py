"""The pins: a hidden path's ancestors cannot be renamed out from under it.

Hiding a path mounts over it, and a mount point cannot be renamed. Its
parent can, where the member has a writable bind above it - a working
directory holding the lease tokens of the project it is in. A member that
renames the parent leaves the next sandbox nothing to hide at the old path
while the secret reads fine under the new one, so every such ancestor is
bound over itself and becomes a mount point too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from specflo.pool.sandbox import base_argv, empty_file, pin_argv, unavailable


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A working directory with the pool's lease tokens under it."""
    project = tmp_path / "project"
    (project / ".specflo" / "leases").mkdir(parents=True)
    (project / ".specflo" / "leases" / "one.token").write_text("a-token", encoding="utf-8")
    (project / "src").mkdir()
    return project


def token(project: Path) -> str:
    return str(project / ".specflo" / "leases" / "one.token")


def test_every_ancestor_inside_the_writable_bind_is_pinned(project) -> None:
    assert pin_argv([str(project)], [token(project)]) == [
        "--bind",
        str(project / ".specflo"),
        str(project / ".specflo"),
        "--bind",
        str(project / ".specflo" / "leases"),
        str(project / ".specflo" / "leases"),
    ]


def test_the_outermost_ancestor_is_pinned_first(project) -> None:
    argv = pin_argv([str(project)], [token(project)])
    assert argv.index(str(project / ".specflo")) < argv.index(
        str(project / ".specflo" / "leases")
    )


def test_the_writable_bind_itself_is_not_pinned(project) -> None:
    assert str(project) not in pin_argv([str(project)], [token(project)])


def test_an_ancestor_outside_every_writable_bind_is_not_pinned(project, tmp_path) -> None:
    assert pin_argv([str(tmp_path / "elsewhere")], [token(project)]) == []


def test_a_hidden_path_with_no_writable_bind_over_it_needs_no_pin(project) -> None:
    assert pin_argv([], [token(project)]) == []


def test_an_ancestor_is_pinned_once_however_many_hidden_paths_share_it(project) -> None:
    other = project / ".specflo" / "leases" / "two.token"
    other.write_text("another", encoding="utf-8")
    argv = pin_argv([str(project)], [token(project), str(other)])
    assert argv.count(str(project / ".specflo" / "leases")) == 2


def test_an_ancestor_that_is_a_symlink_is_not_pinned(tmp_path) -> None:
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "link").symlink_to(root / "real")
    hidden = root / "link" / "secret"
    hidden.write_text("s", encoding="utf-8")
    assert pin_argv([str(root)], [str(hidden)]) == []


def test_an_ancestor_missing_on_the_host_is_not_pinned(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert pin_argv([str(root)], [str(root / "gone" / "secret")]) == []


def test_the_pins_come_before_the_hidden_mounts_they_hold_up(tmp_path, project) -> None:
    empty = empty_file(tmp_path / "state")
    argv = base_argv(hidden=[token(project)], empty=empty, writable=[str(project)])
    assert argv.index("--bind") < argv.index("--ro-bind", argv.index("--bind"))
    assert argv[-1] == "--"


def run_inside(argv: list[str], script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*argv, "/bin/sh", "-c", script], capture_output=True, text=True, timeout=60
    )


def sandbox(tmp_path: Path, project: Path) -> list[str]:
    return base_argv(
        hidden=[token(project)],
        empty=empty_file(tmp_path / "state"),
        writable=[str(project)],
    )


def test_a_member_may_still_write_inside_the_bind(tmp_path, project) -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    made = run_inside(sandbox(tmp_path, project), f"touch {project}/src/new && echo ok")
    assert made.returncode == 0, made.stderr
    assert made.stdout.strip() == "ok"


def test_a_member_cannot_rename_an_ancestor_of_a_hidden_path(tmp_path, project) -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    argv = sandbox(tmp_path, project)
    for ancestor in (project / ".specflo", project / ".specflo" / "leases"):
        moved = run_inside(argv, f"mv {ancestor} {ancestor}-moved 2>&1")
        assert moved.returncode != 0, f"{ancestor} was renamed from inside"
        assert ancestor.is_dir(), f"{ancestor} is gone from the host"


def test_the_token_is_still_hidden_after_a_member_has_tried(tmp_path, project) -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    run_inside(sandbox(tmp_path, project), f"mv {project}/.specflo {project}/moved 2>&1")
    read = run_inside(sandbox(tmp_path, project), f"cat {token(project)}")
    assert read.returncode == 0, read.stderr
    assert read.stdout == ""
    assert Path(token(project)).read_text(encoding="utf-8") == "a-token"
