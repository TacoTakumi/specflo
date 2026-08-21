"""Cross-platform advisory file locking for specflo's on-disk artifacts.

A single :func:`locked` context manager serializes concurrent CLI processes
over a lock file at an **explicitly given path** (the caller derives where the
lock lives; missing parent directories are created on demand). Stdlib-only:
``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows, both with
platform-gated imports. On a platform with neither API it degrades to an
unlocked no-op with a warning — there is no way to lock there, and failing
silently would reintroduce the duplicate-ID/lost-write hazard this module
exists to prevent.

Locking rules (research-grounded, see the project brainstorm):

- Lock a dedicated lock file, never the target artifact: locks bind to
  open files/inodes, and specflo rewrites the target on every write, so a
  lock on the target would be left behind when its inode is swapped.
- Never unlink, rename, or replace the lock file: unlinking splits waiters
  across inodes and breaks mutual exclusion. The lock file is created on
  demand and left in place.
- The lock must span the caller's entire read-compute-write critical section:
  callers acquire before reading the artifact and release after writing it.
- Contention is bounded: acquire non-blocking, poll at ~50ms, give up with a
  loud :class:`~specflo.errors.SpecfloError` after ~10s (overridable per
  call). Blocking indefinitely risks wedging the CLI on a hung process.

``msvcrt.locking`` acts from the current file position for both lock and
unlock, so the handle is re-seeked to offset 0 before each.
"""

from __future__ import annotations

import contextlib
import os
import time
import warnings

from specflo.errors import SpecfloError

try:
    import fcntl as _fcntl  # noqa: PLC0415 - platform-gated
except ImportError:  # pragma: no cover - non-POSIX
    _fcntl = None

try:
    import msvcrt as _msvcrt  # noqa: PLC0415 - platform-gated
except ImportError:  # pragma: no cover - non-Windows
    _msvcrt = None

POLL_INTERVAL = 0.05  # seconds between non-blocking acquisition attempts
LOCK_TIMEOUT = 10.0  # default seconds before giving up and raising

_warned_degrade = False


@contextlib.contextmanager
def locked(lock_path, timeout: float | None = None):
    """Serialize a critical section over the lock file at exactly *lock_path*.

    Missing parent directories of *lock_path* are created on demand. Yields
    while holding an advisory lock; the lock is held for the entire ``with``
    block, so the caller's whole read-compute-write runs inside it. On
    contended acquisition the call polls the non-blocking lock at ~50ms
    intervals for at most *timeout* seconds (default :data:`LOCK_TIMEOUT`)
    and then raises :class:`SpecfloError` naming the lock path.
    """
    deadline = time.monotonic() + (LOCK_TIMEOUT if timeout is None else timeout)
    timeout_value = LOCK_TIMEOUT if timeout is None else timeout
    if _fcntl is not None:
        with _locked_flock(lock_path, deadline, timeout_value):
            yield
    elif _msvcrt is not None:
        with _locked_msvcrt(lock_path, deadline, timeout_value):
            yield
    else:  # pragma: no cover - platforms with neither fcntl nor msvcrt
        global _warned_degrade
        if not _warned_degrade:
            _warned_degrade = True
            warnings.warn(
                "No fcntl or msvcrt available on this platform: file locking is "
                "disabled, so concurrent specflo invocations may mint duplicate "
                "IDs or lose writes.",
                RuntimeWarning,
                stacklevel=2,
            )
        yield


def _open_lock(lock_path):
    """Open (creating on demand) the lock file, and any missing parent dirs."""
    parent = os.path.dirname(os.fspath(lock_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return open(lock_path, "a+b")


def _timeout_error(lock_path, timeout: float) -> SpecfloError:
    return SpecfloError(
        f"Could not lock {lock_path} within {timeout:.1f}s - another specflo "
        "process appears to be writing this artifact. Wait for it to finish "
        "and retry."
    )


def _acquire_poll(attempt, lock_path, deadline: float, timeout: float):
    """Poll *attempt* (a zero-arg callable raising OSError while contended)
    until it succeeds or *deadline* passes. *timeout* is the configured value,
    used only for the error message."""
    while True:
        try:
            attempt()
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise _timeout_error(lock_path, timeout) from None
            time.sleep(POLL_INTERVAL)


@contextlib.contextmanager
def _locked_flock(lock_path, deadline: float, timeout: float):
    fd = _open_lock(lock_path)
    try:
        _acquire_poll(
            lambda: _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB),
            lock_path,
            deadline,
            timeout,
        )
        yield
    finally:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            fd.close()


@contextlib.contextmanager
def _locked_msvcrt(lock_path, deadline: float, timeout: float):
    fd = _open_lock(lock_path)
    try:
        def attempt():
            os.lseek(fd.fileno(), 0, os.SEEK_SET)
            _msvcrt.locking(fd.fileno(), _msvcrt.LK_NBLCK, 1)

        _acquire_poll(attempt, lock_path, deadline, timeout)
        yield
    finally:
        try:
            os.lseek(fd.fileno(), 0, os.SEEK_SET)
            _msvcrt.locking(fd.fileno(), _msvcrt.LK_UNLCK, 1)
        finally:
            fd.close()
