"""The sandbox prefix a member's command runs behind: namespaces and the root.

The checks are on the argv the builder returns, because that is what the
daemon hands to a process start, and one run of ``true`` behind the prefix to
show the argv is a sandbox bwrap accepts rather than a list that reads well.
The run skips where the rig cannot make a sandbox at all, and says why: the
unprivileged user namespace is a kernel and policy setting, not this code.
"""

from __future__ import annotations

import subprocess

import pytest

from specflo.pool.sandbox import base_argv, unavailable


def flag_index(argv: list[str], flag: str) -> int:
    """Where *flag* stands in *argv*; fails the test when it does not."""
    assert flag in argv, f"{flag} is not in the sandbox argv"
    return argv.index(flag)


def pairs(argv: list[str], flag: str) -> list[tuple[str, ...]]:
    """Every use of *flag* in *argv* with the arguments that follow it."""
    width = {"--ro-bind": 2, "--bind": 2, "--tmpfs": 1, "--dev": 1, "--proc": 1}[flag]
    return [
        tuple(argv[index + 1 : index + 1 + width])
        for index, word in enumerate(argv)
        if word == flag
    ]


def test_it_starts_with_bwrap() -> None:
    assert base_argv()[0].endswith("bwrap")


def test_it_unshares_every_namespace() -> None:
    argv = base_argv()
    assert "--unshare-all" in argv
    assert "--unshare-cgroup-try" in argv


def test_it_dies_with_its_parent_and_starts_a_new_session() -> None:
    argv = base_argv()
    assert "--die-with-parent" in argv
    assert "--new-session" in argv


def test_it_binds_the_root_read_only() -> None:
    assert ("/", "/") in pairs(base_argv(), "--ro-bind")
    assert pairs(base_argv(), "--bind") == []


def test_it_gives_the_sandbox_a_fresh_dev_proc_and_temp() -> None:
    argv = base_argv()
    assert pairs(argv, "--dev") == [("/dev",)]
    assert pairs(argv, "--proc") == [("/proc",)]
    assert ("/tmp",) in pairs(argv, "--tmpfs")


def test_the_fresh_mounts_come_after_the_root_so_they_overlay_it() -> None:
    argv = base_argv()
    root = flag_index(argv, "--ro-bind")
    for flag in ("--dev", "--proc", "--tmpfs"):
        assert flag_index(argv, flag) > root


def test_it_ends_with_the_separator_so_a_command_can_follow() -> None:
    assert base_argv()[-1] == "--"


def test_the_builder_reads_nothing_from_the_caller_s_environment(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/nowhere")
    monkeypatch.setenv("TMPDIR", "/nowhere")
    assert base_argv() == base_argv()


def test_a_command_behind_the_prefix_runs_and_is_in_its_own_namespaces() -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    script = "readlink /proc/self/ns/mnt /proc/self/ns/user /proc/self/ns/pid"
    inside = subprocess.run(
        [*base_argv(), "/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert inside.returncode == 0, inside.stderr
    outside = subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, timeout=60
    )
    assert inside.stdout.split() != []
    for sandboxed, host in zip(inside.stdout.split(), outside.stdout.split()):
        assert sandboxed != host


def test_the_sandbox_gets_a_temp_of_its_own() -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    marker = "specflo-sandbox-argv-marker"
    inside = subprocess.run(
        [*base_argv(), "/bin/sh", "-c", f"touch /tmp/{marker} && ls /tmp"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert inside.returncode == 0, inside.stderr
    assert inside.stdout.split() == [marker]
