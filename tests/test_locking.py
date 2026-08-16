"""Tests for the locked() advisory-lock context manager (src/specflo/locking.py).

Covers REQ-01..REQ-04: stdlib-only cross-platform sibling-lock file, full
read-compute-write span, never unlink the lock file, bounded-wait contention
with a loud SpecfloError on timeout.
"""

import ast
import inspect
import multiprocessing
import sys
import time

import pytest

from specflo import locking
from specflo.errors import SpecfloError

from conftest import executable_identifiers


def test_lock_file_is_sibling_and_target_untouched(tmp_path):
    target = tmp_path / "artifact.md"
    assert not target.exists()
    with locking.locked(target):
        assert (tmp_path / "artifact.md.lock").exists()
    # locking never creates or writes the target itself, and never unlinks
    # the lock file (it persists after release, by design).
    assert not target.exists()
    assert (tmp_path / "artifact.md.lock").exists()


def test_second_process_blocked_until_release_then_sees_full_write(tmp_path):
    """A second process's whole critical section is excluded while the holder
    runs its read-compute-write; once the holder releases, the waiter enters
    and reads the fully-written artifact (REQ-02: lock spans the write)."""
    target = tmp_path / "artifact.md"
    target.write_text("")

    held = multiprocessing.Event()
    release = multiprocessing.Event()
    entered = multiprocessing.Event()
    out = multiprocessing.Queue()

    def holder():
        with locking.locked(target):
            held.set()
            release.wait(10)
            # read-compute-write inside the critical section
            doc = target.read_text()
            target.write_text(doc + "holder-entry\n")
        out.put("holder-done")

    def waiter():
        held.wait(10)
        t0 = time.monotonic()
        with locking.locked(target):
            waited = time.monotonic() - t0
            content = target.read_text()
            entered.set()
            out.put(("waited", waited, "holder-entry" in content))
        out.put("waiter-done")

    p1 = multiprocessing.Process(target=holder)
    p2 = multiprocessing.Process(target=waiter)
    p1.start()
    assert held.wait(10), "holder never acquired the lock"
    p2.start()

    time.sleep(0.5)  # waiter polls at ~50ms; it must still be excluded
    assert not entered.is_set(), "waiter entered while the lock was held"

    release.set()
    assert entered.wait(10), "waiter never entered after the release"

    p1.join(10)
    p2.join(10)
    assert p1.exitcode == 0 and p2.exitcode == 0

    results = [out.get(timeout=5) for _ in range(3)]  # holder-done + waiter tuple + waiter-done
    messages = {r for r in results if isinstance(r, str)}
    assert messages == {"holder-done", "waiter-done"}
    waited = [r[1] for r in results if isinstance(r, tuple)][0]
    saw_write = [r[2] for r in results if isinstance(r, tuple)][0]
    assert waited >= 0.3, f"waiter was not genuinely blocked (waited {waited:.2f}s)"
    assert saw_write, "waiter read the artifact before the holder's write completed"


def test_lock_released_on_body_exception(tmp_path):
    target = tmp_path / "artifact.md"
    with pytest.raises(RuntimeError):
        with locking.locked(target):
            raise RuntimeError("boom")
    # a subsequent acquisition must succeed: the lock was released
    with locking.locked(target, timeout=1.0):
        pass


def test_timeout_raises_specflo_error_naming_lock_path(tmp_path):
    target = tmp_path / "artifact.md"
    with locking.locked(target):
        with pytest.raises(SpecfloError) as ei:
            with locking.locked(target, timeout=0.2):
                pass
        assert "artifact.md.lock" in str(ei.value)
        assert "specflo" in str(ei.value).lower()


def test_held_lock_blocks_a_second_handle_in_same_process(tmp_path):
    """Two open file descriptions on the same lock file contend even within
    one process (flock binds to the open file, not the process): a held flock
    excludes a second locked() acquisition, which times out loudly; once the
    first handle is released, a fresh acquisition succeeds."""
    fcntl = pytest.importorskip("fcntl")
    target = tmp_path / "artifact.md"
    fd = open(f"{target}.lock", "a+b")
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(SpecfloError):
            with locking.locked(target, timeout=0.2):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    # the lock is not wedged: a fresh acquisition succeeds
    with locking.locked(target, timeout=1.0):
        pass


def test_default_timeout_is_around_ten_seconds():
    assert locking.LOCK_TIMEOUT == pytest.approx(10.0)


def test_module_never_unlinks_renames_or_replaces_the_lock_path():
    code = executable_identifiers(locking)
    for banned in ("unlink", "remove", "rename"):
        assert banned not in code, f"locking module must never call {banned}()"


def test_module_imports_only_stdlib_and_specflo():
    tree = ast.parse(inspect.getsource(locking))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"specflo"}
    assert imported <= allowed, f"non-stdlib imports: {imported - allowed}"


def test_degrades_to_unlocked_with_warning_when_no_lock_api(tmp_path, monkeypatch):
    monkeypatch.setattr(locking, "_fcntl", None)
    monkeypatch.setattr(locking, "_msvcrt", None)
    target = tmp_path / "artifact.md"
    with pytest.warns(RuntimeWarning):
        with locking.locked(target):
            pass
    # no lock file is even created on the degrade path
    assert not (tmp_path / "artifact.md.lock").exists()


def test_posix_prefers_fcntl_over_msvcrt():
    if sys.platform == "win32":
        assert locking._msvcrt is not None
        assert locking._fcntl is None
    else:
        assert locking._fcntl is not None
        assert locking._msvcrt is None
