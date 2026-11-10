"""The release check: `specflo check-update` and the startup notice.

No test here reaches the network: the one function that fetches from PyPI is
replaced, and the release cache lives in the test's temporary directory.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from specflo import __version__, guide
from specflo.cli import build_cli


def _newer(version: str) -> str:
    major, *rest = version.split(".")
    return ".".join([str(int(major) + 1), *rest])


@pytest.fixture
def pypi(monkeypatch, tmp_path):
    """Replace the PyPI fetch; ``pypi.answer`` is a version, or an exception to raise."""
    from agentsquire import release

    class Pypi:
        answer: object = __version__
        urls: list[str] = []

    state = Pypi()
    state.urls = []

    def fetch(url):
        state.urls.append(url)
        if isinstance(state.answer, BaseException):
            raise state.answer
        return json.dumps({"info": {"version": state.answer}}).encode()

    monkeypatch.setattr(release, "_fetch_body", fetch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return state


def test_check_update_names_a_newer_release(pypi):
    pypi.answer = _newer(__version__)
    result = CliRunner().invoke(build_cli(), ["check-update"])
    assert result.exit_code == 0
    assert f"specflo: a new release is available: {__version__} -> {pypi.answer}" in result.output
    assert pypi.urls == ["https://pypi.org/pypi/specflo/json"]


def test_check_update_says_when_specflo_is_up_to_date(pypi):
    result = CliRunner().invoke(build_cli(), ["check-update"])
    assert result.exit_code == 0
    assert f"specflo {__version__} is up to date" in result.output


def test_check_update_fails_with_the_reason_when_pypi_is_out_of_reach(pypi):
    pypi.answer = OSError("no route to host")
    result = CliRunner().invoke(build_cli(), ["check-update"])
    assert result.exit_code == 1
    assert "specflo: could not check for a new release" in result.output


def test_check_update_runs_when_the_notice_is_switched_off(pypi, monkeypatch):
    # The variable silences the startup notice. A user who types the command
    # asked for the check.
    monkeypatch.setenv("AGENTSQUIRE_NO_UPDATE_CHECK", "1")
    result = CliRunner().invoke(build_cli(), ["check-update"])
    assert result.exit_code == 0
    assert len(pypi.urls) == 1


def test_the_guide_lists_check_update():
    entries = {entry["name"]: entry for entry in guide.COMMANDS}
    assert "check-update" in entries
    assert "PyPI" in entries["check-update"]["summary"]
