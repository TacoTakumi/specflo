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
    summary = entries["check-update"]["summary"]
    assert "PyPI" in summary
    # `specflo doctor` asks PyPI too, so the row claims no "only".
    assert "the one command" not in summary and "only" not in summary


# --- the startup notice -------------------------------------------------------


@pytest.fixture
def cache(monkeypatch, tmp_path):
    """A release cache in the test's directory, the notice on, and no network."""
    from agentsquire import release

    def refuse(url):
        raise AssertionError(f"the startup notice made a request: {url}")

    monkeypatch.setattr(release, "_fetch_body", refuse)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("AGENTSQUIRE_NO_UPDATE_CHECK", raising=False)

    def write(version):
        from agentsquire import _release_cache

        _release_cache.write_cache("specflo", version)

    return write


def _run_main(monkeypatch, capsys, *args):
    from specflo import cli

    monkeypatch.setattr("sys.argv", ["specflo", *args])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    captured = capsys.readouterr()
    return exit_info.value.code, captured.out, captured.err


NOTICE = "specflo: a new release is available"


def test_the_notice_names_a_newer_release_on_stderr_only(cache, monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    quiet = _run_main(monkeypatch, capsys, "--version")
    assert NOTICE not in quiet[2]

    cache(_newer(__version__))
    code, out, err = _run_main(monkeypatch, capsys, "--version")
    assert f"{NOTICE}: {__version__} -> {_newer(__version__)}" in err
    assert (code, out) == (quiet[0], quiet[1])


def test_the_notice_is_silent_when_the_cached_release_is_not_newer(
    cache, monkeypatch, capsys, tmp_path
):
    monkeypatch.chdir(tmp_path)
    cache(__version__)
    assert NOTICE not in _run_main(monkeypatch, capsys, "--version")[2]


@pytest.mark.parametrize("variable", ["CI", "AGENTSQUIRE_NO_UPDATE_CHECK"])
def test_the_notice_is_off_when_a_variable_switches_it_off(
    variable, cache, monkeypatch, capsys, tmp_path
):
    monkeypatch.chdir(tmp_path)
    cache(_newer(__version__))
    monkeypatch.setenv(variable, "1")
    assert NOTICE not in _run_main(monkeypatch, capsys, "--version")[2]


@pytest.mark.parametrize("before", [(), ("-C", ".")], ids=["first", "after-an-option"])
def test_check_update_prints_the_fresh_answer_without_the_cached_notice(
    before, cache, pypi, monkeypatch, capsys, tmp_path
):
    # The cache can be older than the answer the command is about to print.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cache(_newer(__version__))
    pypi.answer = __version__
    code, out, err = _run_main(monkeypatch, capsys, *before, "check-update")
    assert code == 0
    assert f"specflo {__version__} is up to date" in out
    assert NOTICE not in err


# --- the suite's own cache ------------------------------------------------------


def _under_home_cache(path):
    from pathlib import Path

    return Path(path).resolve().is_relative_to((Path.home() / ".cache").resolve())


def test_the_suite_reads_no_release_cache_of_the_developer():
    # A newer release recorded on this machine would add a stderr line to
    # every test that reads a command's stderr.
    from agentsquire import _release_cache

    assert not _under_home_cache(_release_cache.cache_path("specflo"))
    assert _release_cache.read_cache("specflo") is None


def test_the_suite_gives_a_subprocess_the_same_cache():
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c",
         "from agentsquire import _release_cache as c; print(c.cache_path('specflo'))"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert not _under_home_cache(out)
