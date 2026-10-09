"""The operator's user-level settings, kept in the home's ``.specflo``.

One file, ``settings.yaml``, holds what belongs to the host rather than to a
checkout. Today that is the daemon root, so ``specflo serve`` finds it without
``--root``. The file is not ``config.yaml``: ``find_root`` walks up to the
first ``.specflo/config.yaml``, and that name in the home would make the home
a specflo root for every directory below it.

The daemon root comes from ``--root``, then the environment, then this file;
the first one set wins, and only the file is ever written.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml

from .config import CONFIG_DIRNAME
from .errors import SpecfloError

SETTINGS_FILENAME = "settings.yaml"
DAEMON_ROOT_KEY = "daemon_root"
DAEMON_ROOT_ENV = "SPECFLO_DAEMON_ROOT"
DEFAULT_DAEMON_ROOT = "~/specflo-daemon"
ROOT_OPTION = "--root"


def settings_path(home: Path | str | None = None) -> Path:
    """Where the settings file is: under *home*, else under the user's home."""
    return Path(home if home is not None else Path.home()) / CONFIG_DIRNAME / SETTINGS_FILENAME


def load_settings(home: Path | str | None = None) -> dict:
    """The settings in the file, or an empty dict when there is no file."""
    path = settings_path(home)
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecfloError(f"{path} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SpecfloError(f"{path} must hold a mapping of settings.")
    return data


def save_daemon_root(root: Path | str, home: Path | str | None = None) -> Path:
    """Save *root*, made absolute, as the daemon root; return it.

    Every other key in the file is kept.
    """
    root = Path(root).expanduser().resolve()
    data = load_settings(home)
    data[DAEMON_ROOT_KEY] = str(root)
    path = settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return root


def resolve_daemon_root(
    cli_root: Path | str | None,
    environ: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> tuple[Path, str] | None:
    """The daemon root in force and where it came from, or None when unset.

    The source is ``--root``, the environment variable's name, or the
    settings file's path.
    """
    if cli_root is not None:
        return Path(cli_root).expanduser(), ROOT_OPTION
    environ = os.environ if environ is None else environ
    if environ.get(DAEMON_ROOT_ENV):
        return Path(environ[DAEMON_ROOT_ENV]).expanduser(), DAEMON_ROOT_ENV
    saved = load_settings(home).get(DAEMON_ROOT_KEY)
    if saved:
        return Path(str(saved)).expanduser(), str(settings_path(home))
    return None
