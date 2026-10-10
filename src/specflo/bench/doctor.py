"""`specflo bench doctor`: check that the model bench is set up on this machine.

Run it once at setup and whenever a bench command fails. Each check is ok,
fail or skip, and every fail names its fix, as in `specflo doctor`. The
doctor reads; it never prompts, writes a file or loads a model. The per-run
preflight stays the run's own guard.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from specflo import settings
from specflo.bench import arms
from specflo.doctor import Check
from specflo.errors import SpecfloError

HOME_DIRNAME = "bench"
SET_ROOT = (
    f"`specflo serve root <dir>`, or pass --root <dir> or set ${settings.DAEMON_ROOT_ENV}"
)


def _root(option: Path | None) -> tuple[Check, Path | None]:
    try:
        found = settings.resolve_daemon_root(option)
    except SpecfloError as exc:
        return Check("fail", "daemon root", str(exc), SET_ROOT), None
    if found is None:
        return Check("fail", "daemon root", "no daemon root is set", SET_ROOT), None
    root, source = found
    return Check("ok", "daemon root", f"{root} (from {source})"), root


def _config(root: Path | None) -> tuple[Check, arms.Config | None]:
    if root is None:
        return Check("skip", "bench config", "no daemon root"), None
    path = root / HOME_DIRNAME / "arms.yaml"
    if not path.is_file():
        return Check("fail", "bench config", f"{path} does not exist", "`specflo bench init`"), None
    try:
        config = arms.load_config(path)
    except (arms.ArmError, yaml.YAMLError, OSError) as exc:
        return Check("fail", "bench config", f"{path}: {exc}", f"edit {path}"), None
    detail = f"{path} ({len(config.entries)} entries, harnesses: {', '.join(config.harnesses)})"
    return Check("ok", "bench config", detail), config


def run_checks(root_option: Path | None) -> list[Check]:
    """Every check, in report order."""
    root_check, root = _root(root_option)
    config_check, _config_found = _config(root)
    return [root_check, config_check]
