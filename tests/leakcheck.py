"""Fail a test that leaves behind a process carrying its tmp_path.

A test that starts a process and returns without stopping it leaks that
process into the rest of the run. At the end of each test that has a
``tmp_path``, once the test's fixtures have torn down, this plugin looks for
this user's processes whose command line (``/proc/<pid>/cmdline``) carries
that path. It gives them ``GRACE_SECONDS`` times the wait scale (see
``waits.scaled``) to exit, then kills the ones still running and fails the
test with the pid and command of each. The failure comes in the teardown, so
pytest reports it as an error at teardown of the test.

Each test's tmp_path is its own, under pytest-xdist too, so a check never
touches the processes of another test. A test that leaks nothing costs one
pass over /proc and no wait. Load the plugin with ``-p leakcheck`` or from
a conftest's ``pytest_plugins``.
"""

import os
import re
import select
import shlex
import signal
import time
from dataclasses import dataclass

import pytest

from waits import SCALE_ENV, scaled

GRACE_SECONDS = 5.0
KILL_SECONDS = 5.0
# Rescans after a kill, for a process that a leaked one started meanwhile.
_ROUNDS = 3
_ENABLED = hasattr(os, "pidfd_open") and os.path.isdir("/proc/self")


@dataclass
class _Proc:
    """A matched process, held by a pidfd so that a reused pid cannot stand in for it."""

    pid: int
    pidfd: int
    comm: str
    command: str

    def exited(self) -> bool:
        """True once the process has exited (a zombie counts)."""
        poller = select.poll()
        poller.register(self.pidfd, select.POLLIN)
        return bool(poller.poll(0))

    def kill(self) -> None:
        """Send SIGKILL, unless the process is already gone."""
        try:
            signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def describe(self) -> str:
        """``pid N (comm): command`` for the failure message."""
        return f"pid {self.pid} ({self.comm}): {self.command}"


def _read(path: str) -> bytes | None:
    """The whole content of a /proc file, or None when it cannot be read."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    return b"".join(chunks)


def _pattern(tmp_path) -> tuple[bytes, re.Pattern]:
    """The path as bytes for a quick substring test, and the exact pattern.

    Another test's tmp_path in the same base dir can start with this one
    (``test_x1`` and ``test_x10``). pytest makes each dir name of word
    characters only, so the path must not run on into one.
    """
    needle = os.fsencode(str(tmp_path))
    return needle, re.compile(re.escape(needle) + rb"(?![\w\x80-\xff])")


def _open(pid: int, pattern: re.Pattern, uid: int) -> _Proc | None:
    """Hold ``pid`` by a pidfd when it is this user's and still carries the path."""
    try:
        pidfd = os.pidfd_open(pid)
    except OSError:
        return None
    try:
        owner = os.stat(f"/proc/{pid}").st_uid
    except OSError:
        owner = None
    # Read the command line again: the pid may have gone to a new process
    # between the scan and the pidfd.
    cmdline = _read(f"/proc/{pid}/cmdline")
    if owner != uid or not cmdline or not pattern.search(cmdline):
        os.close(pidfd)
        return None
    args = [arg.decode(errors="backslashreplace") for arg in cmdline.rstrip(b"\0").split(b"\0")]
    comm = (_read(f"/proc/{pid}/comm") or b"?").decode(errors="backslashreplace").strip()
    return _Proc(pid, pidfd, comm, shlex.join(args))


def _scan(needle: bytes, pattern: re.Pattern, skip=frozenset()) -> list[_Proc]:
    """This user's live processes whose command line carries the path."""
    own, uid = os.getpid(), os.getuid()
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == own or pid in skip:
            continue
        cmdline = _read(f"/proc/{name}/cmdline")
        if not cmdline or needle not in cmdline or not pattern.search(cmdline):
            continue
        if proc := _open(pid, pattern, uid):
            found.append(proc)
    return found


def _wait(procs: list[_Proc], seconds: float) -> None:
    """Wait up to ``seconds`` for every process in ``procs`` to exit."""
    poller = select.poll()
    waiting = {proc.pidfd for proc in procs}
    for pidfd in waiting:
        poller.register(pidfd, select.POLLIN)
    deadline = time.monotonic() + seconds
    while waiting:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        for pidfd, _ in poller.poll(int(remaining * 1000) + 1):
            waiting.discard(pidfd)
            poller.unregister(pidfd)


def _kill(found: list[_Proc], needle: bytes, pattern: re.Pattern) -> list[_Proc]:
    """Kill what still runs of ``found`` and any new process that carries the path.

    Returns the processes it killed. It closes every pidfd it holds.
    """
    held = list(found)
    killed: list[_Proc] = []
    try:
        live = [proc for proc in found if not proc.exited()]
        for _ in range(_ROUNDS):
            fresh = _scan(needle, pattern, skip={proc.pid for proc in held})
            held += fresh
            live += fresh
            if not live:
                break
            for proc in live:
                proc.kill()
            killed += live
            _wait(live, scaled(KILL_SECONDS))
            live = []
        for proc in killed:
            if not proc.exited():
                proc.command += "  [still running after SIGKILL]"
        return killed
    finally:
        for proc in held:
            os.close(proc.pidfd)


def reap(tmp_path) -> str | None:
    """Wait for the processes that carry ``tmp_path``, then kill the ones left.

    Returns a message that names each killed process, or None when there was
    none left to kill.
    """
    if not _ENABLED:
        return None
    needle, pattern = _pattern(tmp_path)
    found = _scan(needle, pattern)
    if not found:
        return None
    grace = scaled(GRACE_SECONDS)
    try:
        _wait(found, grace)
    finally:
        # Also when a timeout interrupts the wait: kill before it goes on.
        killed = _kill(found, needle, pattern)
    if not killed:
        return None
    count = f"{len(killed)} process" + ("es" if len(killed) != 1 else "")
    lines = [
        f"leaked {count} with {tmp_path} in the command line, still running"
        f" {grace:g} s after the test (grace {GRACE_SECONDS:g} s x {SCALE_ENV}"
        f" {scaled(1.0):g}); killed:"
    ]
    lines += [f"  {proc.describe()}" for proc in killed]
    return "\n".join(lines)


def _tmp_path(item):
    """The test's tmp_path, when the test has one that was set up."""
    if "tmp_path" not in getattr(item, "fixturenames", ()):
        return None
    return (getattr(item, "funcargs", None) or {}).get("tmp_path")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Check for leaked processes once the test's fixtures have torn down."""
    # Take the value now: pytest clears funcargs after the teardown.
    tmp_path = _tmp_path(item)
    if tmp_path is None:
        return (yield)
    try:
        result = yield
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        # A fixture teardown failed: keep its error and add the leak to it.
        if leaked := reap(tmp_path):
            exc.add_note(leaked)
        raise
    if leaked := reap(tmp_path):
        pytest.fail(leaked, pytrace=False)
    return result
