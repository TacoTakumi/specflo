"""The resource limits: set by prlimit in front of the sandbox, never in a fork.

The daemon is threaded, and a callback between fork and exec in a threaded
process is documented as unsafe. So the limits are a program in front of the
sandbox: prlimit sets them on itself and execs bwrap, and nothing of the
daemon's own runs in the child. The last check reads every start path in the
tree to see that none of them takes the other road.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from specflo.pool.sandbox import Limits, base_argv, limit_argv, unavailable

SRC = Path(__file__).resolve().parents[2] / "src" / "specflo"


def flags(argv: list[str]) -> dict[str, int]:
    return {
        word.split("=")[0]: int(word.split("=")[1]) for word in argv if word.startswith("--")
    }


def test_it_carries_the_address_space_the_cpu_and_the_process_limits() -> None:
    argv = limit_argv(Limits(memory_mb=512, cpu_seconds=60, max_procs=32))
    assert argv[0].endswith("prlimit")
    assert flags(argv)["--as"] == 512 * 1024 * 1024
    assert flags(argv)["--cpu"] == 60
    assert "--nproc" in flags(argv)


def test_the_process_limit_stands_above_what_the_user_already_runs() -> None:
    argv = limit_argv(Limits(max_procs=32))
    assert flags(argv)["--nproc"] > 32


def test_a_limit_left_out_sets_nothing() -> None:
    assert limit_argv(Limits(memory_mb=512)).count("--cpu") == 0
    assert limit_argv(Limits()) == []


def test_a_limit_above_the_inherited_hard_limit_is_clamped() -> None:
    # The hard limit is lowered in a child, since a process cannot raise its
    # own back afterwards and every later test would inherit the lower one.
    asked = 3600
    read = subprocess.run(
        [
            "prlimit",
            f"--cpu={asked // 2}",
            sys.executable,
            "-c",
            "from specflo.pool.sandbox import Limits, limit_argv;"
            f" print(limit_argv(Limits(cpu_seconds={asked})))",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert read.returncode == 0, read.stderr
    assert f"--cpu={asked // 2}" in read.stdout


def test_the_built_argv_begins_with_the_limits_and_the_sandbox_follows() -> None:
    argv = base_argv(limits=Limits(memory_mb=512))
    assert argv[0].endswith("prlimit")
    assert argv[1] == f"--as={512 * 1024 * 1024}"
    assert argv[2].endswith("bwrap")


def test_a_member_with_no_limits_is_still_sandboxed() -> None:
    assert base_argv()[0].endswith("bwrap")


def _start_calls(path: Path) -> list[ast.Call]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            found.append(node)
    return found


def test_no_start_path_in_the_tree_passes_a_pre_exec_callback() -> None:
    sources = sorted(SRC.rglob("*.py"))
    assert sources, "sources not found"
    passing = [
        f"{path.relative_to(SRC).as_posix()}:{call.lineno}"
        for path in sources
        for call in _start_calls(path)
        for keyword in call.keywords
        if keyword.arg == "preexec_fn"
    ]
    assert passing == []


def run_inside(argv: list[str], script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*argv, "/bin/sh", "-c", script], capture_output=True, text=True, timeout=120
    )


ALLOCATE = "exec python3 -c 'import sys; b = bytearray(600 * 1024 * 1024); sys.exit(0)'"


def test_a_member_that_asks_for_more_than_its_address_space_does_not_get_it() -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    held = run_inside(base_argv(limits=Limits(memory_mb=256)), ALLOCATE)
    assert held.returncode != 0
    loose = run_inside(base_argv(limits=Limits(memory_mb=4096)), ALLOCATE)
    assert loose.returncode == 0, loose.stderr
