"""The daemon root is set once per host and found without --root.

The root comes from --root, then the environment, then the user settings
file in the home's .specflo; with none of them a terminal is asked and the
answer saved, and anything else is refused with the command that sets it.
"""

import pytest
import yaml

from specflo import settings
from specflo.config import find_root


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
