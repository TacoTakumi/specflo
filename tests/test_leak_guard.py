"""The leak guard fails a test that leaves something of its own behind.

It records, before and after each test, the member scopes, the live child
processes, the unix sockets and the run scratch dirs of the test, and fails
the test at teardown on any that is new and still there. It stops nothing:
what it finds stays as it is, for the test's own cleanup to end.

Each test plants a small suite under the gateway tests' real conftest and
runs it in a pytest subprocess (an empty ini keeps the repo's settings and
plugins out), so the planted leftovers stay out of this run. What a planted
test leaves is written down, and each test here ends it by pid, unit name or
path in a finally.
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import pytest

import leak_guard
from specflo.pool import sandbox
from waits import SCALE_ENV, scaled

TESTS_DIR = Path(__file__).resolve().parent
GATEWAY_CONFTEST = TESTS_DIR / "gateway" / "conftest.py"

# Each planted test writes what it leaves to the record, one "kind value" line.
PLANTED = """
import os
import socket
import subprocess
import tempfile
from pathlib import Path

import leak_guard

RECORD = Path({record!r})
OUTSIDE = Path({outside!r})
HELD = []


def note(kind, value):
    with RECORD.open("a") as out:
        out.write(f"{{kind}} {{value}}\\n")


def test_leaves_a_child():
    proc = subprocess.Popen(["sleep", "600"])
    note("pid", proc.pid)


def test_leaves_a_scratch_dir():
    note("dir", tempfile.mkdtemp(prefix=leak_guard.scratch_prefix()))


def test_leaves_a_socket_listening():
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(OUTSIDE))
    listener.listen()
    HELD.append(listener)
    note("socket", OUTSIDE)


# A socket path may hold 107 bytes, and a tmp_path here is longer: the
# sockets under it are bound by a path relative to it.
def test_leaves_a_socket_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "daemon").mkdir()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind("daemon/lease-1.sock")
    listener.close()
    note("socket", tmp_path / "daemon" / "lease-1.sock")


def test_cleans_up_after_itself(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    proc = subprocess.Popen(["sleep", "600"])
    try:
        scratch = tempfile.mkdtemp(prefix=leak_guard.scratch_prefix())
        os.rmdir(scratch)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind("lease-2.sock")
        listener.listen()
        listener.close()
        os.unlink("lease-2.sock")
    finally:
        proc.kill()
        proc.wait()
"""

# The planted test starts a sleep in a member scope of its own name and
# returns once the scope is up.
PLANTED_SCOPE = """
import subprocess
import time
from pathlib import Path

RECORD = Path({record!r})
UNIT = {unit!r}


def test_leaves_a_member_scope():
    proc = subprocess.Popen(
        ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={{UNIT}}",
         "--", "sleep", "600"]
    )
    RECORD.write_text(f"pid {{proc.pid}}\\n")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = subprocess.run(
            ["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True
        ).stdout.strip()
        if state == "active":
            return
        time.sleep(0.05)
    raise AssertionError("the scope never came up")
"""

# Two modules that leave the same child: the guard made with ``only`` checks
# the first one alone.
PLANTED_ONLY = """
import subprocess
from pathlib import Path

RECORD = Path({record!r})


def test_leaves_a_child():
    proc = subprocess.Popen(["sleep", "600"])
    with RECORD.open("a") as out:
        out.write(f"pid {{proc.pid}}\\n")
"""


def recorded(record: Path, kind: str) -> list[str]:
    """The values of *kind* that the planted tests wrote down."""
    if not record.exists():
        return []
    lines = record.read_text().splitlines()
    return [line.split(" ", 1)[1] for line in lines if line.split(" ", 1)[0] == kind]


def running(pid: int) -> bool:
    """True while *pid* runs and is not a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def end_recorded(record: Path) -> None:
    """End what the planted tests wrote down: each process by pid, each dir and socket by path."""
    for pid in recorded(record, "pid"):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    for path in recorded(record, "dir"):
        shutil.rmtree(path, ignore_errors=True)
    for path in recorded(record, "socket"):
        Path(path).unlink(missing_ok=True)


@pytest.fixture
def short_dir():
    """A dir of a short path, for a socket bound outside the planted tests' tmp_path."""
    path = Path(tempfile.mkdtemp(prefix="leakguard-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def planted(pytester, monkeypatch):
    """Plant a suite under the gateway conftest; run it; end what it left at teardown."""
    record = pytester.path / "left.txt"
    pytester.makeini("[pytest]\n")
    monkeypatch.setenv("PYTHONPATH", str(TESTS_DIR))
    # Each leftover is waited for before it counts; a short wait keeps this fast.
    monkeypatch.setenv(SCALE_ENV, "0.2")

    def run(conftest: str, **modules: str):
        pytester.makeconftest(conftest)
        pytester.makepyfile(**modules)
        return pytester.runpytest_subprocess("-p", "no:cacheprovider", timeout=scaled(60))

    try:
        yield record, run
    finally:
        end_recorded(record)


def test_each_leftover_fails_its_test_and_is_left_as_it_is(planted, short_dir):
    record, run = planted
    outside = short_dir / "outside.sock"
    result = run(
        GATEWAY_CONFTEST.read_text(),
        test_planted=PLANTED.format(record=str(record), outside=str(outside)),
    )

    result.assert_outcomes(passed=5, errors=4)
    (pid,) = recorded(record, "pid")
    (scratch,) = recorded(record, "dir")
    assert Path(scratch).parent == Path(tempfile.gettempdir())
    result.stdout.fnmatch_lines_random(
        [
            "*ERROR at teardown of test_leaves_a_child*",
            f"*child process pid {pid} (sleep): sleep 600*",
            "*ERROR at teardown of test_leaves_a_scratch_dir*",
            f"*run scratch dir {scratch}*",
            "*ERROR at teardown of test_leaves_a_socket_listening*",
            f"*unix socket {outside}*",
            "*ERROR at teardown of test_leaves_a_socket_file*",
            "*unix socket */daemon/lease-1.sock*",
        ]
    )
    assert "ERROR at teardown of test_cleans_up_after_itself" not in result.stdout.str()
    # The guard stops nothing and removes nothing.
    assert running(int(pid))
    assert Path(scratch).is_dir()


def test_a_run_scratch_dir_is_named_for_the_process_that_makes_it():
    # The gateway makes each run's scratch dir with this prefix in the system
    # temp dir, so the guard finds the dirs of its own process among those
    # that the other test workers make meanwhile.
    assert leak_guard.scratch_prefix() == f"specflo-run-{os.getpid()}-"
    assert leak_guard.scratch_prefix(42) == "specflo-run-42-"


def test_a_member_scope_left_up_fails_the_test(planted, pytester):
    reason = sandbox.scope_fault(os.environ)
    if reason is not None:
        pytest.skip(f"no user manager to make a scope with: {reason}")
    record, run = planted
    unit = f"{sandbox.SCOPE_PREFIX}guardtest-{secrets.token_hex(4)}.scope"
    try:
        result = run(
            GATEWAY_CONFTEST.read_text(),
            test_planted=PLANTED_SCOPE.format(record=str(record), unit=unit),
        )
        result.assert_outcomes(passed=1, errors=1)
        result.stdout.fnmatch_lines([f"*member scope {unit}*"])
        state = subprocess.run(
            ["systemctl", "--user", "is-active", unit], capture_output=True, text=True
        ).stdout.strip()
        assert state == "active", "the guard stopped the scope"
    finally:
        subprocess.run(
            ["systemctl", "--user", "stop", unit], capture_output=True, timeout=scaled(30)
        )


def test_a_guard_for_named_modules_checks_those_alone(planted):
    record, run = planted
    conftest = (
        "import leak_guard\n\n"
        "leak_guarded = leak_guard.autouse(only={'test_guarded.py'})\n"
    )
    module = PLANTED_ONLY.format(record=str(record))
    result = run(conftest, test_guarded=module, test_unguarded=module)

    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["ERROR test_guarded.py::test_leaves_a_child*"])
    assert "ERROR test_unguarded.py" not in result.stdout.str()


def test_the_pool_guard_names_the_web_proxy_tests():
    from pool import conftest

    assert conftest.LEAK_GUARDED == {
        "test_webproxy.py", "test_webproxy_log.py", "test_webproxy_lease.py",
    }


def test_a_clean_run_records_nothing_new(tmp_path):
    before = leak_guard.snapshot(tmp_path)
    after = leak_guard.snapshot(tmp_path)
    assert leak_guard.leftovers(before, after) == []
