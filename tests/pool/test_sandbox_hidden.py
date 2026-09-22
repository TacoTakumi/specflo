"""What the sandbox hides: the operator's own directories and the sockets.

The hidden set is worked out from the daemon's environment and then read
back twice - once as argv, which is what a start hands over, and once from
inside a real sandbox, which is what a member sees. The second read is the
one that matters, and it skips where the rig cannot make a sandbox at all.

Every path here is under a temporary home, so the checks never touch the
operator's own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from specflo.pool.sandbox import (
    base_argv,
    empty_file,
    hidden_argv,
    operator_paths,
    unavailable,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home laid out like the operator's, with something in each corner."""
    home = tmp_path / "home"
    for relative in (".pi/agent", ".agents/skills/a-skill", ".specflo/agents/one"):
        (home / relative).mkdir(parents=True)
    (home / ".pi/agent/auth.json").write_text('{"key": "secret"}', encoding="utf-8")
    (home / ".agents/skills/a-skill/SKILL.md").write_text("marker", encoding="utf-8")
    (home / ".specflo/agents/one/sock").write_text("", encoding="utf-8")
    (home / "work").mkdir()
    return home


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    """A runtime directory laid out like the operator's, with a bus in it."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "bus").write_text("", encoding="utf-8")
    return runtime


@pytest.fixture
def environ(home: Path, runtime: Path) -> dict[str, str]:
    return {"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime)}


def test_it_hides_the_home_the_configuration_the_skills_and_the_tokens(
    environ, home
) -> None:
    paths = operator_paths(environ)
    for path in (home, home / ".pi", home / ".agents", home / ".specflo"):
        assert str(path) in paths


def test_it_hides_the_runtime_directory_the_sockets_are_in(environ, runtime) -> None:
    assert str(runtime) in operator_paths(environ)


def test_the_home_comes_before_what_is_under_it(environ, home) -> None:
    paths = operator_paths(environ)
    assert paths.index(str(home)) < paths.index(str(home / ".pi"))


def test_the_configuration_directory_the_environment_names_is_the_one_hidden(
    environ, tmp_path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    environ["PI_CODING_AGENT_DIR"] = str(elsewhere)
    assert str(elsewhere) in operator_paths(environ)


def test_a_path_is_hidden_at_its_real_place(environ, home, tmp_path) -> None:
    target = tmp_path / "real-agents"
    target.mkdir()
    shutil.rmtree(home / ".agents")
    (home / ".agents").symlink_to(target)
    paths = operator_paths(environ)
    assert str(target) in paths
    assert str(home / ".agents") not in paths


def test_a_directory_gets_a_tmpfs_and_a_file_gets_an_empty_file_over_it(
    tmp_path, home
) -> None:
    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")
    argv = hidden_argv([str(home / ".pi"), str(home / ".pi/agent/auth.json")], empty)
    assert argv == [
        "--tmpfs",
        str(home / ".pi"),
        "--ro-bind",
        str(empty),
        str(home / ".pi/agent/auth.json"),
    ]


def test_a_path_that_is_not_on_the_host_is_left_out(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")
    assert hidden_argv([str(tmp_path / "never-was")], empty) == []


def test_a_path_that_became_a_symlink_is_left_out(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")
    planted = tmp_path / "planted"
    planted.symlink_to(tmp_path)
    assert hidden_argv([str(planted)], empty) == []


def test_the_empty_file_is_made_beside_the_state_it_is_named_for(tmp_path) -> None:
    made = empty_file(tmp_path / "state")
    assert made.is_file()
    assert made.stat().st_size == 0
    assert made.parent == tmp_path / "state"


def test_the_hidden_mounts_come_after_the_root_bind(tmp_path, environ) -> None:
    empty = empty_file(tmp_path / "state")
    argv = base_argv(hidden=operator_paths(environ), empty=empty)
    assert argv.index("--ro-bind") < argv.index("--tmpfs")
    assert argv[-1] == "--"


def read_inside(argv: list[str], script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*argv, "/bin/sh", "-c", script], capture_output=True, text=True, timeout=60
    )


def test_a_member_finds_each_hidden_directory_empty_and_the_host_does_not(
    tmp_path, environ, home, runtime
) -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    empty = empty_file(tmp_path / "state")
    argv = base_argv(hidden=operator_paths(environ), empty=empty)
    for directory in (home / ".pi", home / ".agents", home / ".specflo", runtime):
        inside = read_inside(argv, f"ls -A {directory}")
        assert inside.returncode == 0, inside.stderr
        assert inside.stdout.strip() == "", f"{directory} is not empty inside"
        assert os.listdir(directory) != [], f"{directory} is empty on the host"
    # The home holds the mount points of the directories under it and nothing
    # else: the operator's own work is gone from it, and each corner a member
    # might look in is there and empty.
    inside = read_inside(argv, f"ls -A {home}")
    assert inside.returncode == 0, inside.stderr
    assert sorted(inside.stdout.split()) == [".agents", ".pi", ".specflo"]
    assert "work" in os.listdir(home)


def test_a_member_cannot_read_a_hidden_file_the_host_still_holds(
    tmp_path, environ, home
) -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    empty = empty_file(tmp_path / "state")
    secret = home / ".pi/agent/auth.json"
    argv = base_argv(hidden=operator_paths(environ), empty=empty)
    inside = read_inside(argv, f"cat {secret} 2>&1; true")
    assert "secret" not in inside.stdout
    assert "secret" in secret.read_text(encoding="utf-8")


def test_a_named_file_outside_the_hidden_directories_reads_empty(tmp_path) -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    token = tmp_path / "a.token"
    token.write_text("a-lease-token", encoding="utf-8")
    empty = empty_file(tmp_path / "state")
    argv = base_argv(hidden=[str(token)], empty=empty)
    inside = read_inside(argv, f"cat {token}")
    assert inside.returncode == 0, inside.stderr
    assert inside.stdout == ""
    assert token.read_text(encoding="utf-8") == "a-lease-token"
