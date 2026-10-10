"""Bench test setup: the bench is the specflo.bench subpackage."""

import pytest

from specflo.bench import CONFIG_ENV, HOME_ENV


@pytest.fixture(autouse=True)
def _no_bench_home(monkeypatch):
    """Read the shipped configs unless a test names a bench home itself."""
    monkeypatch.delenv(HOME_ENV, raising=False)
    monkeypatch.delenv(CONFIG_ENV, raising=False)
