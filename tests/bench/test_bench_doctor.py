"""`specflo bench doctor`: the bench setup check, one line per check."""

import json

import pytest
from typer.testing import CliRunner

from specflo import settings
from specflo.bench import DATA
from specflo.bench import doctor as bench_doctor
from specflo.cli import app

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A daemon root R in the environment; the bench home is R/bench."""
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv(settings.DAEMON_ROOT_ENV, str(tmp_path / "R"))
    bench = tmp_path / "R" / "bench"
    bench.mkdir(parents=True)
    return bench


def _by_subject(checks, subject):
    return [c for c in checks if c.subject == subject]


def test_report_fails_with_a_fix_and_exit_1_when_a_check_fails(home):
    result = runner.invoke(app, ["bench", "doctor"])

    assert result.exit_code == 1
    assert any(line.startswith("FAIL") and "Fix:" in line for line in result.stdout.splitlines())


def test_report_json_carries_ok_and_every_check(home):
    result = runner.invoke(app, ["bench", "doctor", "--json"])

    data = json.loads(result.stdout)
    assert result.exit_code == 1
    assert data["ok"] is False
    assert {"status", "subject", "detail", "fix"} <= set(data["checks"][0])


def test_report_exits_0_when_no_check_fails(home, monkeypatch):
    monkeypatch.setattr(
        bench_doctor, "run_checks", lambda root, **kw: [bench_doctor.Check("ok", "x", "fine")]
    )

    result = runner.invoke(app, ["bench", "doctor"])

    assert result.exit_code == 0, result.output
    assert "All checks passed." in result.stdout


def test_root_unset_fails_naming_serve_root_without_a_prompt(home, monkeypatch):
    monkeypatch.delenv(settings.DAEMON_ROOT_ENV)
    monkeypatch.setattr("specflo.daemon.cli._stdin_is_terminal", lambda: True)

    result = runner.invoke(app, ["bench", "doctor"], input="")

    assert result.exit_code == 1
    assert "Daemon root" not in result.output
    [root] = [line for line in result.stdout.splitlines() if "  daemon root:" in line]
    assert root.startswith("FAIL") and "specflo serve root" in root


def test_root_set_is_ok_and_names_its_source(home):
    [check] = _by_subject(bench_doctor.run_checks(None), "daemon root")

    assert check.status == "ok"
    assert str(home.parent) in check.detail
    assert settings.DAEMON_ROOT_ENV in check.detail


def test_config_valid_file_is_ok(home):
    (home / "arms.yaml").write_text((DATA / "arms.yaml").read_text())

    [check] = _by_subject(bench_doctor.run_checks(None), "bench config")

    assert check.status == "ok"


def test_config_missing_fails_naming_bench_init(home):
    [check] = _by_subject(bench_doctor.run_checks(None), "bench config")

    assert check.status == "fail"
    assert "specflo bench init" in check.fix


def test_config_without_entries_fails_naming_the_field(home):
    (home / "arms.yaml").write_text("harnesses: [pi]\nlevels: {quick: {}}\n")

    [check] = _by_subject(bench_doctor.run_checks(None), "bench config")

    assert check.status == "fail"
    assert "entries" in check.detail
