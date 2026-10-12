"""Fail a test that leaves something of its own running or behind.

The gateway's tests and the web proxy's tests start member scopes, child
processes, unix sockets and run scratch dirs, and each of them must be gone
when the test ends, also when it fails. Around each guarded test this module
records what of those belongs to the test, once before it and once after its
fixtures have torn down, and fails the test at teardown with each one that is
new and still there:

- member scopes: the user's systemd units named ``sandbox.SCOPE_PREFIX``
  ``*.scope`` that are this test's, because a process in the scope descends
  from the test process or carries its tmp_path, or the scope's description
  (the command line it was started for) carries the tmp_path;
- child processes: the live (not zombie) processes that descend from the
  test process;
- unix sockets: a socket file anywhere under the test's tmp_path, and every
  unix socket with a path that the test process holds open, wherever it is;
- run scratch dirs: the dirs in the system temp dir whose name starts with
  ``scratch_prefix()``, ``specflo-run-<pid>-``. The gateway makes each run's
  scratch dir with ``tempfile.mkdtemp(prefix=f"specflo-run-{os.getpid()}-")``,
  so the pid names the process that made it, and the guard finds those of its
  own process among the dirs that the other test workers make meanwhile.

A leftover gets ``GRACE_SECONDS`` times the wait scale to go before it counts,
for a process that was killed and is still exiting or a scope that systemd has
not yet collected. The guard stops nothing and removes nothing: it reports,
and the test's own cleanup is what must end each thing. A conftest takes the
guard with ``leak_guarded = leak_guard.autouse()``, or with ``only=`` naming
the test modules it covers.
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from specflo.pool import sandbox
from waits import SCALE_ENV, scaled

SCRATCH_PREFIX = "specflo-run-"
GRACE_SECONDS = 3.0
POLL_SECONDS = 0.05
SYSTEMCTL_SECONDS = 10.0


def scratch_prefix(pid: int | None = None) -> str:
    """The name prefix of the run scratch dirs that process *pid* makes (this one by default)."""
    return f"{SCRATCH_PREFIX}{os.getpid() if pid is None else pid}-"


@dataclass(frozen=True)
class Snapshot:
    """What of each kind belongs to the test at one moment, each with how it reads."""

    scopes: dict[str, str] = field(default_factory=dict)
    children: dict[tuple[int, str], str] = field(default_factory=dict)
    sockets: dict[str, str] = field(default_factory=dict)
    scratch: dict[str, str] = field(default_factory=dict)


def _read(path: str) -> bytes:
    """The whole content of a /proc or /sys file, or nothing when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def _stat_fields(pid: int) -> tuple[str, int, str, str] | None:
    """The comm, parent pid, state and start time of *pid*, or None when it is gone."""
    raw = _read(f"/proc/{pid}/stat").decode(errors="replace")
    if ")" not in raw:
        return None
    comm = raw[raw.index("(") + 1 : raw.rindex(")")]
    rest = raw.rsplit(")", 1)[1].split()
    # state is field 3 and ppid field 4 of stat; starttime is field 22.
    return comm, int(rest[1]), rest[0], rest[19]


def _command(pid: int) -> str:
    """The command line of *pid*, its arguments joined by spaces."""
    return _read(f"/proc/{pid}/cmdline").rstrip(b"\0").replace(b"\0", b" ").decode(
        errors="backslashreplace"
    )


def _descendants() -> dict[int, tuple[str, str, str]]:
    """Every process that descends from this one, by pid, with its comm, state and start."""
    parents: dict[int, list[int]] = {}
    info: dict[int, tuple[str, str, str]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        fields = _stat_fields(int(name))
        if fields is None:
            continue
        comm, ppid, state, start = fields
        parents.setdefault(ppid, []).append(int(name))
        info[int(name)] = (comm, state, start)
    found: dict[int, tuple[str, str, str]] = {}
    stack = [os.getpid()]
    while stack:
        for child in parents.get(stack.pop(), []):
            if child not in found and child in info:
                found[child] = info[child]
                stack.append(child)
    return found


def _children(descendants: dict[int, tuple[str, str, str]]) -> dict[tuple[int, str], str]:
    """The live descendants, by pid and start time, so a reused pid is a new process."""
    return {
        (pid, start): f"child process pid {pid} ({comm}): {_command(pid)}"
        for pid, (comm, state, start) in descendants.items()
        if state != "Z"
    }


def _member_scopes(tmp_path: Path, descendants: Collection[int]) -> dict[str, str]:
    """The member scopes that are this test's, by unit name.

    None where systemctl cannot reach the user manager: no member starts in a
    scope there.
    """
    try:
        listed = subprocess.run(
            ["systemctl", "--user", "show", "-p", "Id", "-p", "ActiveState",
             "-p", "ControlGroup", "-p", "Description", f"{sandbox.SCOPE_PREFIX}*.scope"],
            capture_output=True, timeout=scaled(SYSTEMCTL_SECONDS),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    mark = os.fsencode(str(tmp_path))
    found = {}
    for block in listed.stdout.split(b"\n\n"):
        fields = dict(line.split(b"=", 1) for line in block.splitlines() if b"=" in line)
        unit = fields.get(b"Id", b"").decode(errors="replace")
        if not unit.startswith(sandbox.SCOPE_PREFIX) or fields.get(b"ActiveState") == b"inactive":
            continue
        group = fields.get(b"ControlGroup", b"").decode(errors="replace").strip("/")
        pids = [int(p) for p in _read(f"/sys/fs/cgroup/{group}/cgroup.procs").split()] if group else []
        if mark in fields.get(b"Description", b"") or any(
            pid in descendants or mark in _read(f"/proc/{pid}/cmdline") for pid in pids
        ):
            state = fields.get(b"ActiveState", b"?").decode(errors="replace")
            found[unit] = f"member scope {unit} ({state})"
    return found


def _held_sockets() -> dict[str, str]:
    """The unix sockets with a path that this process holds open, by path."""
    inodes = set()
    with contextlib.suppress(OSError):
        for fd in os.listdir("/proc/self/fd"):
            with contextlib.suppress(OSError):
                target = os.readlink(f"/proc/self/fd/{fd}")
                if target.startswith("socket:["):
                    inodes.add(target[len("socket:[") : -1])
    found = {}
    for line in _read("/proc/net/unix").decode(errors="replace").splitlines()[1:]:
        parts = line.split(None, 7)
        # Num RefCount Protocol Flags Type St Inode Path; an abstract one starts with @.
        if len(parts) == 8 and parts[6] in inodes and not parts[7].startswith("@"):
            found[parts[7]] = f"unix socket {parts[7]} (held open)"
    return found


def _socket_files(tmp_path: Path) -> dict[str, str]:
    """The socket files under *tmp_path*, by path."""
    found = {}
    for root, _dirs, files in os.walk(tmp_path):
        for name in files:
            path = os.path.join(root, name)
            with contextlib.suppress(OSError):
                if stat.S_ISSOCK(os.lstat(path).st_mode):
                    found[path] = f"unix socket {path} (file left)"
    return found


def _scratch_dirs() -> dict[str, str]:
    """The run scratch dirs that this process made, by path."""
    base = tempfile.gettempdir()
    prefix = scratch_prefix()
    try:
        names = os.listdir(base)
    except OSError:
        return {}
    return {
        os.path.join(base, name): f"run scratch dir {os.path.join(base, name)}"
        for name in names
        if name.startswith(prefix)
    }


def snapshot(tmp_path: Path) -> Snapshot:
    """What of each kind belongs to the test with *tmp_path* now."""
    descendants = _descendants()
    return Snapshot(
        scopes=_member_scopes(tmp_path, descendants),
        children=_children(descendants),
        sockets={**_held_sockets(), **_socket_files(tmp_path)},
        scratch=_scratch_dirs(),
    )


def leftovers(before: Snapshot, after: Snapshot) -> list[str]:
    """How each thing in *after* that was not in *before* reads, scopes first."""
    found = []
    for kind in ("scopes", "children", "sockets", "scratch"):
        old, new = getattr(before, kind), getattr(after, kind)
        found += [new[key] for key in new if key not in old]
    return found


def check(tmp_path: Path, before: Snapshot) -> list[str]:
    """The leftovers still there once the grace time has passed; none at once when clean."""
    deadline = time.monotonic() + scaled(GRACE_SECONDS)
    while True:
        left = leftovers(before, snapshot(tmp_path))
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(POLL_SECONDS)


@contextlib.contextmanager
def watching(tmp_path: Path) -> Iterator[None]:
    """Fail when what runs inside leaves a member scope, child, socket or scratch dir."""
    before = snapshot(tmp_path)
    yield
    if left := check(tmp_path, before):
        lines = [
            f"left behind by this test, still there {scaled(GRACE_SECONDS):g} s after it"
            f" (grace {GRACE_SECONDS:g} s x {SCALE_ENV} {scaled(1.0):g});"
            " the guard stops nothing, so the test's cleanup must end each:"
        ]
        lines += [f"  {line}" for line in left]
        pytest.fail("\n".join(lines), pytrace=False)


def autouse(only: Collection[str] | None = None):
    """An autouse fixture that guards each test, or only those of the modules named in *only*."""

    @pytest.fixture(autouse=True)
    def leak_guarded(request):
        if only is not None and request.path.name not in only:
            yield
            return
        with watching(request.getfixturevalue("tmp_path")):
            yield

    return leak_guarded
