"""The leak check fails a test that leaves a process carrying its tmp_path.

Each test plants a small suite and runs it in a pytest subprocess that loads
only the leakcheck plugin (an empty ini keeps the repo's settings out), so
the planted processes and waits stay out of this run.
"""

import os
import signal
from pathlib import Path

import pytest

from waits import SCALE_ENV, scaled

TESTS_DIR = Path(__file__).resolve().parent

# The planted test starts ``sleep`` with a path under its tmp_path as argv[0]
# (sleep would reject the path as an extra argument), writes the pid down
# and returns without stopping it.
PLANTED = """
import subprocess
from pathlib import Path

PID_FILE = Path({pid_file!r})


def running(pid):
    try:
        return bool(Path(f"/proc/{{pid}}/cmdline").read_bytes())
    except FileNotFoundError:
        return False


def test_starts_a_sleep(tmp_path):
    proc = subprocess.Popen([str(tmp_path / "sleeper"), "{seconds}"], executable="sleep")
    PID_FILE.write_text(str(proc.pid))


def test_runs_after_it():
    assert not running(int(PID_FILE.read_text())), "the sleep still runs"
"""


@pytest.fixture
def run_planted(pytester, monkeypatch):
    """Run a planted test that leaves a sleep of the given seconds; return the result and pid.

    At teardown it kills the sleep if the check under test left it running.
    """
    pid_file = pytester.path / "sleep.pid"
    pytester.makeini("[pytest]\n")
    monkeypatch.setenv("PYTHONPATH", str(TESTS_DIR))

    def run(seconds):
        pytester.makepyfile(test_planted=PLANTED.format(pid_file=str(pid_file), seconds=seconds))
        result = pytester.runpytest_subprocess("-p", "leakcheck", timeout=scaled(60))
        return result, int(pid_file.read_text())

    yield run
    if pid_file.exists() and _carries(pid := int(pid_file.read_text()), pytester.path):
        os.kill(pid, signal.SIGKILL)


def _carries(pid, path):
    """True while ``pid`` runs with ``path`` in its command line."""
    try:
        return os.fsencode(str(path)) in Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return False


def test_a_process_left_running_fails_the_test_and_is_killed(run_planted, pytester, monkeypatch):
    # A 600 s sleep outlives any grace time, so a short one keeps this fast.
    monkeypatch.setenv(SCALE_ENV, "0.2")
    result, pid = run_planted(600)
    # The test body passes and its teardown fails; the next test sees the
    # sleep gone.
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at teardown of test_starts_a_sleep*",
            "*leaked 1 process with */test_starts_a_sleep0 in the command line*",
            f"*pid {pid} (sleep): */test_starts_a_sleep0/sleeper 600",
        ]
    )
    assert not _carries(pid, pytester.path)


def test_a_process_that_exits_within_the_grace_time_passes(run_planted):
    # The next test sees the sleep gone, so the check waited for it to exit.
    result, pid = run_planted(1)
    result.assert_outcomes(passed=2)
    assert "leaked" not in result.stdout.str()


def test_the_grace_time_scales_with_the_wait_scale(run_planted, pytester, monkeypatch):
    # 5 s x 0.1 is 0.5 s, shorter than the 2 s sleep.
    monkeypatch.setenv(SCALE_ENV, "0.1")
    result, pid = run_planted(2)
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(
        [
            f"*still running 0.5 s after the test (grace 5 s x {SCALE_ENV} 0.1); killed:",
            f"*pid {pid} (sleep): */sleeper 2",
        ]
    )
    assert not _carries(pid, pytester.path)
