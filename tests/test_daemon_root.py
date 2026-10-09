"""The daemon root is set once per host and found without --root.

The root comes from --root, then the environment, then the user settings
file in the home's .specflo; with none of them a terminal is asked and the
answer saved, and anything else is refused with the command that sets it.
"""

import pytest
import yaml
from typer.testing import CliRunner

from specflo import settings
from specflo.cli import app
from specflo.config import find_root
from specflo.daemon import auth
from specflo.daemon import cli as serve_cli

runner = CliRunner()


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


def _save(home, root):
    path = settings.settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({settings.DAEMON_ROOT_KEY: str(root)}))


# --- resolve ----------------------------------------------------------------


def test_resolve_prefers_the_option_over_env_and_file(home, tmp_path):
    _save(home, tmp_path / "saved")
    env = {settings.DAEMON_ROOT_ENV: str(tmp_path / "env")}

    found = settings.resolve_daemon_root(tmp_path / "opt", environ=env, home=home)

    assert found == (tmp_path / "opt", "--root")


def test_resolve_prefers_the_env_over_the_file(home, tmp_path):
    _save(home, tmp_path / "saved")
    env = {settings.DAEMON_ROOT_ENV: str(tmp_path / "env")}

    found = settings.resolve_daemon_root(None, environ=env, home=home)

    assert found == (tmp_path / "env", settings.DAEMON_ROOT_ENV)


def test_resolve_reads_the_file_last(home, tmp_path):
    _save(home, tmp_path / "saved")

    found = settings.resolve_daemon_root(None, environ={}, home=home)

    assert found == (tmp_path / "saved", str(settings.settings_path(home)))


def test_resolve_gives_none_when_nothing_is_set(home):
    assert settings.resolve_daemon_root(None, environ={}, home=home) is None


def test_resolve_save_writes_an_absolute_root_and_keeps_other_keys(home, tmp_path, monkeypatch):
    path = settings.settings_path(home)
    path.parent.mkdir(parents=True)
    path.write_text("other: kept\n")
    monkeypatch.chdir(tmp_path)

    saved = settings.save_daemon_root("rel", home=home)

    assert saved == (tmp_path / "rel").resolve()
    assert path == home / ".specflo" / "settings.yaml"
    assert yaml.safe_load(path.read_text()) == {"other": "kept", "daemon_root": str(saved)}


def test_resolve_settings_file_does_not_make_the_home_a_specflo_root(home, tmp_path):
    settings.save_daemon_root(tmp_path / "d", home=home)
    below = home / "work" / "repo"
    below.mkdir(parents=True)

    assert find_root(below) is None


# --- serve ------------------------------------------------------------------


@pytest.fixture
def cli_home(home, monkeypatch):
    """The home every serve call in the test reads, with no root in the env."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(settings.DAEMON_ROOT_ENV, raising=False)
    return home


def _serve(*args):
    return runner.invoke(app, ["serve", *args])


def _has_tokens(root):
    return (root / auth.TOKENS_FILENAME).is_file()


def test_serve_option_wins_and_is_not_saved(cli_home, tmp_path, monkeypatch):
    _save(cli_home, tmp_path / "saved")
    monkeypatch.setenv(settings.DAEMON_ROOT_ENV, str(tmp_path / "env"))
    before = settings.settings_path(cli_home).read_text()

    result = _serve("--root", str(tmp_path / "opt"), "token", "add", "requester")

    assert result.exit_code == 0, result.output
    assert _has_tokens(tmp_path / "opt")
    assert not _has_tokens(tmp_path / "env") and not _has_tokens(tmp_path / "saved")
    assert settings.settings_path(cli_home).read_text() == before


def test_serve_env_wins_over_the_saved_root(cli_home, tmp_path, monkeypatch):
    _save(cli_home, tmp_path / "saved")
    monkeypatch.setenv(settings.DAEMON_ROOT_ENV, str(tmp_path / "env"))

    result = _serve("token", "add", "requester")

    assert result.exit_code == 0, result.output
    assert _has_tokens(tmp_path / "env") and not _has_tokens(tmp_path / "saved")


def test_serve_reads_the_saved_root(cli_home, tmp_path):
    _save(cli_home, tmp_path / "saved")

    result = _serve("token", "add", "requester")

    assert result.exit_code == 0, result.output
    assert _has_tokens(tmp_path / "saved")


@pytest.mark.parametrize("answer", ["typed", ""])
def test_serve_asks_a_terminal_and_saves_the_answer(cli_home, tmp_path, monkeypatch, answer):
    monkeypatch.setattr(serve_cli, "_stdin_is_terminal", lambda: True)
    typed = tmp_path / "typed"
    expected = typed if answer else cli_home / "specflo-daemon"

    result = runner.invoke(
        app, ["serve", "token", "add", "requester"], input=f"{typed if answer else ''}\n"
    )

    assert result.exit_code == 0, result.output
    assert "Daemon root" in result.output
    assert _has_tokens(expected)
    saved = yaml.safe_load(settings.settings_path(cli_home).read_text())
    assert saved == {settings.DAEMON_ROOT_KEY: str(expected.resolve())}


def test_serve_off_a_terminal_fails_with_the_command_that_sets_the_root(cli_home):
    result = _serve("token", "add", "requester")

    assert result.exit_code == 1
    assert "specflo serve root" in result.stderr
    assert not settings.settings_path(cli_home).exists()


# --- serve root -------------------------------------------------------------


def test_root_command_saves_then_shows_the_root_and_its_file(cli_home, tmp_path):
    saved = _serve("root", str(tmp_path / "chosen"))
    shown = _serve("root")

    assert saved.exit_code == 0, saved.output
    data = yaml.safe_load(settings.settings_path(cli_home).read_text())
    assert data == {settings.DAEMON_ROOT_KEY: str((tmp_path / "chosen").resolve())}
    assert shown.exit_code == 0, shown.output
    assert shown.stdout.strip() == str((tmp_path / "chosen").resolve())
    assert str(settings.settings_path(cli_home)) in shown.stderr


def test_root_command_names_the_env_when_it_is_in_force(cli_home, tmp_path, monkeypatch):
    monkeypatch.setenv(settings.DAEMON_ROOT_ENV, str(tmp_path / "env"))

    shown = _serve("root")

    assert shown.stdout.strip() == str(tmp_path / "env")
    assert settings.DAEMON_ROOT_ENV in shown.stderr


def test_root_command_with_nothing_set_says_so_without_asking(cli_home, monkeypatch):
    monkeypatch.setattr(serve_cli, "_stdin_is_terminal", lambda: True)

    shown = _serve("root")

    assert shown.exit_code == 0, shown.output
    assert "No daemon root is set" in shown.stderr
    assert "Daemon root" not in shown.output
    assert not settings.settings_path(cli_home).exists()


# --- help -------------------------------------------------------------------


@pytest.mark.parametrize("terminal", [True, False])
@pytest.mark.parametrize("command", [["token", "add"], ["pool", "validate"]])
def test_help_on_a_subcommand_needs_no_root(cli_home, monkeypatch, terminal, command):
    monkeypatch.setattr(serve_cli, "_stdin_is_terminal", lambda: terminal)

    result = _serve(*command, "--help")

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.stdout
    assert "Daemon root" not in result.output
    assert not settings.settings_path(cli_home).exists()


def test_first_run_prompt_keeps_stdout_to_the_secret(cli_home, tmp_path, monkeypatch):
    monkeypatch.setattr(serve_cli, "_stdin_is_terminal", lambda: True)

    result = runner.invoke(
        app, ["serve", "token", "add", "requester"], input=f"{tmp_path / 'typed'}\n"
    )

    assert result.exit_code == 0, result.output
    assert "Daemon root" in result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    assert auth.identity_for(tmp_path / "typed", lines[0]) == "requester"
