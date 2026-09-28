"""A test that hangs fails at its time limit, and the run goes on.

pyproject.toml gives every test a limit through pytest-timeout with the signal
method. This plants a small suite with a hanging test under a 2 s limit and
checks what that setting promises: the hanging test fails with a timeout, its
fixture teardown runs, and the next test still runs.

The planted suite runs in a subprocess. The signal method needs the main
thread and SIGALRM, and an in-process run would share both with this test,
whose own limit rides on the same alarm. A subprocess has its own, with or
without pytest-xdist.
"""

PLANTED = """
    import time
    from pathlib import Path

    import pytest

    HERE = Path(__file__).parent


    @pytest.fixture
    def held(request):
        request.addfinalizer(lambda: (HERE / "finalizer-ran").touch())


    def test_hangs(held):
        time.sleep(600)


    def test_after():
        (HERE / "after-ran").touch()
"""


def test_hanging_test_fails_at_its_limit_and_the_run_goes_on(pytester):
    # An empty ini pins the planted run's config, so it never picks up this
    # repo's; the timeout options go on the command line.
    pytester.makeini("[pytest]\n")
    pytester.makepyfile(test_planted=PLANTED)

    result = pytester.runpytest_subprocess(
        "--timeout=2", "--timeout-method=signal", timeout=60
    )

    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*_ test_hangs _*",
            "E * Failed: Timeout (>2.0s) from pytest-timeout.",
            "FAILED test_planted.py::test_hangs - *",
        ]
    )
    assert (pytester.path / "finalizer-ran").exists()
    assert (pytester.path / "after-ran").exists()
