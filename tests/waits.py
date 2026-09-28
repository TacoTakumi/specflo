"""Wait in tests with limits that a slow or loaded machine can stretch.

``wait_until`` polls a condition and returns as soon as it holds. ``settle``
lets time pass, so that something a test says must not happen has its chance
to happen. ``scaled`` gives any other limit (a thread join, a subprocess
timeout, a CLI ``--timeout``) the same stretch.

Each of them multiplies its seconds by the ``SPECFLO_TEST_WAIT_SCALE``
environment variable (a number above 0, default 1), read at each call. A run
on a slow machine sets it to give every wait more room without a test edit.
"""

import os
import time

SCALE_ENV = "SPECFLO_TEST_WAIT_SCALE"


def _scale() -> float:
    """The factor in ``SPECFLO_TEST_WAIT_SCALE``: 1 when it is unset or empty."""
    raw = os.environ.get(SCALE_ENV, "").strip()
    if not raw:
        return 1.0
    try:
        factor = float(raw)
    except ValueError:
        raise ValueError(f"{SCALE_ENV} must be a number, not {raw!r}") from None
    if not factor > 0:
        raise ValueError(f"{SCALE_ENV} must be above 0, not {raw!r}")
    return factor


def scaled(seconds: float) -> float:
    """``seconds`` times the wait scale, for a limit this module does not wait on."""
    return seconds * _scale()


def wait_until(cond, timeout: float = 30.0, *, interval: float = 0.05, message: str | None = None):
    """Poll ``cond`` until it returns a truthy value, and return that value.

    The first check runs at once and the last at the scaled ``timeout``. A
    condition that has not held by then raises AssertionError with
    ``message``, or the condition's name when there is no message. An
    exception from ``cond`` goes through at once.
    """
    factor = _scale()
    limit = timeout * factor
    deadline = time.monotonic() + limit
    while True:
        value = cond()
        if value:
            return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            what = message or getattr(cond, "__qualname__", repr(cond))
            raise AssertionError(
                f"{what}: not true within {limit:g} s"
                f" (timeout {timeout:g} s x {SCALE_ENV} {factor:g})"
            )
        time.sleep(min(interval, remaining))


def settle(seconds: float) -> None:
    """Let the scaled ``seconds`` pass before the test looks again."""
    time.sleep(scaled(seconds))
