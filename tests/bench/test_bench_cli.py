"""`specflo bench`: the bench commands with their paths under the bench home."""

import importlib

import pytest
from typer.testing import CliRunner

from specflo import settings
from specflo.bench import DATA
from specflo.cli import app

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A daemon root R in the environment; the bench home is R/bench."""
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv(settings.DAEMON_ROOT_ENV, str(tmp_path / "R"))
    return tmp_path / "R" / "bench"


@pytest.fixture
def seen(monkeypatch):
    """Each bench command's main, recording the argv it is called with."""
    calls: dict[str, list[str]] = {}
    for name in ("run", "batch", "records", "probe", "grade", "graph"):
        module = importlib.import_module(f"specflo.bench.cmd_{name}")
        monkeypatch.setattr(module, "main", lambda argv, name=name: calls.setdefault(name, argv) and 0)
    return calls


def _bench(*args, input=None):
    return runner.invoke(app, ["bench", *args], input=input)


def _flags(argv):
    """The value of each --flag in argv; the last one wins, as in argparse."""
    values = {}
    for flag, value in zip(argv, argv[1:]):
        if flag.startswith("--"):
            values[flag] = value
    return values


def test_bench_help_lists_every_command():
    result = _bench("--help")

    assert result.exit_code == 0
    for name in ("init", "run", "batch", "records", "probe", "grade", "graph"):
        assert name in result.stdout


def test_init_writes_the_templates_and_keeps_an_edited_file(home):
    first = _bench("init")
    (home / "arms.yaml").write_text("edited\n")
    second = _bench("init")

    assert first.exit_code == 0, first.output
    assert (home / "matrices.yaml").read_text() == (DATA / "matrices.yaml").read_text()
    assert (home / "arms.yaml").read_text() == "edited\n"
    assert f"kept {home / 'arms.yaml'}" in second.stdout
    assert all((home / d).is_dir() for d in ("runs", "results", "state"))


def test_with_no_root_off_a_terminal_it_fails_naming_serve_root(home, monkeypatch):
    monkeypatch.delenv(settings.DAEMON_ROOT_ENV)

    result = _bench("init")

    assert result.exit_code == 1
    assert "specflo serve root" in result.stderr


def test_root_option_names_the_daemon_root(home, tmp_path):
    result = _bench("--root", str(tmp_path / "other"), "init")

    assert result.exit_code == 0, result.output
    assert (tmp_path / "other" / "bench" / "arms.yaml").is_file()


@pytest.mark.parametrize(
    "command, expected",
    [
        ("run", {"--config": "arms.yaml", "--state": "state/loaded-by-bench.json", "--runs-dir": "runs",
                 "--home": "."}),
        ("batch", {"--config": "arms.yaml", "--matrices": "matrices.yaml",
                   "--state": "state/loaded-by-bench.json", "--runs-dir": "runs", "--home": "."}),
        ("records", {"--config": "arms.yaml", "--matrices": "matrices.yaml", "--runs-dir": "runs"}),
        ("probe", {"--config": "arms.yaml", "--state": "state/loaded-by-bench.json"}),
        ("graph", {"--config": "arms.yaml", "--records": "runs", "--out": "results/latest"}),
    ],
)
def test_each_command_defaults_its_paths_to_the_bench_home(home, seen, command, expected):
    result = _bench(command)

    assert result.exit_code == 0, result.output
    flags = _flags(seen[command])
    assert {flag: flags[flag] for flag in expected} == {
        flag: str(home / path) if path != "." else str(home) for flag, path in expected.items()
    }


def test_grade_takes_nothing_from_the_home(home, seen):
    result = _bench("grade", "--tree", "t", "--level", "quick")

    assert result.exit_code == 0, result.output
    assert seen["grade"] == ["--tree", "t", "--level", "quick"]


def test_a_flag_on_the_command_line_wins(home, seen, tmp_path):
    result = _bench("run", "--config", str(tmp_path / "mine.yaml"), "--runs-dir=" + str(tmp_path / "r"))

    assert result.exit_code == 0, result.output
    argv = seen["run"]
    assert _flags(argv)["--config"] == str(tmp_path / "mine.yaml")
    assert "--runs-dir" not in argv and "--runs-dir=" + str(tmp_path / "r") in argv


def test_graph_writes_under_the_named_results_directory(home, seen):
    result = _bench("graph", "pilot", "--arm", "e:pi")

    assert result.exit_code == 0, result.output
    assert _flags(seen["graph"])["--out"] == str(home / "results" / "pilot")
    assert seen["graph"][-2:] == ["--arm", "e:pi"]


def test_graph_out_on_the_command_line_wins(home, seen, tmp_path):
    result = _bench("graph", "--out", str(tmp_path / "o"))

    assert result.exit_code == 0, result.output
    assert seen["graph"].count("--out") == 1
    assert _flags(seen["graph"])["--out"] == str(tmp_path / "o")


def test_help_on_a_command_needs_no_root(home, monkeypatch):
    monkeypatch.delenv(settings.DAEMON_ROOT_ENV)

    result = _bench("graph", "--help")

    assert result.exit_code == 0, result.output
    assert "usage: specflo bench graph" in result.stdout
    assert not settings.settings_path().exists()


def test_init_writes_the_pi_model_specs_and_keeps_an_edited_copy(home):
    first = _bench("init")
    (home / "pi-models.json").write_text("{}\n")
    second = _bench("init")

    assert first.exit_code == 0 and second.exit_code == 0
    assert (home / "pi-models.json").read_text() == "{}\n"
    assert f"kept {home / 'pi-models.json'}" in second.stdout


def test_the_command_runs_with_the_bench_home_in_its_environment(home, monkeypatch):
    import os

    from specflo.bench import HOME_ENV, cmd_records

    monkeypatch.delenv(HOME_ENV, raising=False)
    seen = {}
    monkeypatch.setattr(cmd_records, "main", lambda argv: seen.setdefault("home", os.environ.get(HOME_ENV)) and 0)

    result = _bench("records")

    assert result.exit_code == 0, result.output
    assert seen["home"] == str(home)


@pytest.mark.parametrize("given", [None, "--config", "--config="])
def test_the_command_runs_with_the_config_in_force_in_its_environment(home, monkeypatch, tmp_path, given):
    import os

    from specflo.bench import CONFIG_ENV, cmd_run

    seen = {}
    monkeypatch.setattr(cmd_run, "main", lambda argv: seen.setdefault("config", os.environ.get(CONFIG_ENV)) and 0)
    mine = tmp_path / "mine.yaml"
    args = {None: [], "--config": ["--config", str(mine)], "--config=": [f"--config={mine}"]}[given]

    result = _bench("run", *args)

    assert result.exit_code == 0, result.output
    assert seen["config"] == str(mine if given else home / "arms.yaml")
    assert CONFIG_ENV not in os.environ
