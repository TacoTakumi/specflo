"""Loading, saving, and scaffolding specflo's per-repo config.

The config lives at ``<root>/.specflo/config.yaml``. Commands locate it by
walking up from the current directory (like git finds ``.git``), so specflo
works from anywhere inside a project tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import SpecfloError

CONFIG_DIRNAME = ".specflo"
CONFIG_FILENAME = "config.yaml"
DEFAULT_PROJECTS_DIR = "docs/projects"


DEFAULT_AUTONOMY = "safe"
# Default iteration/step cap for `specflo auto` (REQ-14): a runaway backstop on
# the unattended pass loop. Single source of truth for the config-field default,
# `resolve_max_passes`, and the CLI help/docs.
DEFAULT_MAX_PASSES = 50
# Default percent of the model context window at which the pi extension *arms*
# its clear-and-continue trigger (pi-extension REQ-28). Arming is not firing: the
# next specflo seam fires it. A percent, never an absolute token count, so it
# holds across models with different window sizes.
DEFAULT_CONTEXT_THRESHOLD_PERCENT = 75
# The usable percent range. 100 is allowed (arm only at a full window, in effect
# disabling the trigger); 0 and below would arm every turn from the start.
CONTEXT_THRESHOLD_RANGE = (1, 100)


@dataclass
class SpecfloConfig:
    projects_dir: str = DEFAULT_PROJECTS_DIR
    active_project: str | None = None
    # Default autonomy level for `specflo auto` when no --autonomy flag is given
    # (REQ-08). A level *string*, never an auto-*on* toggle (REQ-01/D-10). Kept as
    # a literal default here to avoid importing `auto` (which imports `config`).
    autonomy: str = DEFAULT_AUTONOMY
    # Default iteration/step cap for `specflo auto` when no --max-passes flag is
    # given (REQ-14). Not an auto-*on* toggle - a numeric backstop.
    auto_max_passes: int = DEFAULT_MAX_PASSES
    # Percent of the model context window at which the pi extension arms its
    # clear-and-continue trigger (pi-extension REQ-28). Surfaced to the extension
    # through `specflo status --json`, so it never parses this file itself.
    context_threshold_percent: int = DEFAULT_CONTEXT_THRESHOLD_PERCENT


def config_path(root: Path) -> Path:
    return root / CONFIG_DIRNAME / CONFIG_FILENAME


def find_root(start: Path) -> Path | None:
    """Walk up from ``start`` to the first directory holding a specflo config."""
    start = Path(start).resolve()
    for directory in (start, *start.parents):
        if config_path(directory).is_file():
            return directory
    return None


def display_path(path: Path, root: Path, *, posix: bool = False) -> str:
    """``path`` expressed relative to the repo ``root``, or absolute if outside it.

    Defaults to the OS-native separator for terminal display; pass ``posix=True``
    for stored artifact references that must read the same on every platform
    (e.g. the paths written into ``checkpoint.md``).
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return path.as_posix() if posix else str(path)
    return rel.as_posix() if posix else str(rel)


def _context_threshold(raw) -> int:
    """``raw`` as a usable arming percent, or the default when it is not one.

    A hand-edited config must not break every command that loads it, so an
    unusable value degrades rather than raising. ``bool`` is rejected explicitly:
    it subclasses ``int``, so ``True`` would otherwise read as the percent 1.
    """
    low, high = CONTEXT_THRESHOLD_RANGE
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        return DEFAULT_CONTEXT_THRESHOLD_PERCENT
    return raw


def load_config(root: Path) -> SpecfloConfig:
    path = config_path(root)
    if not path.is_file():
        raise SpecfloError(
            f"No specflo project here ({path} not found). Run `specflo init` first."
        )
    data = yaml.safe_load(path.read_text()) or {}
    return SpecfloConfig(
        projects_dir=data.get("projects_dir", DEFAULT_PROJECTS_DIR),
        active_project=data.get("active_project"),
        autonomy=data.get("autonomy", DEFAULT_AUTONOMY),
        auto_max_passes=data.get("auto_max_passes", DEFAULT_MAX_PASSES),
        context_threshold_percent=_context_threshold(
            data.get("context_threshold_percent", DEFAULT_CONTEXT_THRESHOLD_PERCENT)
        ),
    )


def save_config(root: Path, cfg: SpecfloConfig) -> None:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"projects_dir": cfg.projects_dir, "active_project": cfg.active_project}
    # Persist the auto-mode defaults only when non-default, so a plain project's
    # config stays minimal and carries no auto-* key at all (REQ-01).
    if cfg.autonomy != DEFAULT_AUTONOMY:
        payload["autonomy"] = cfg.autonomy
    if cfg.auto_max_passes != DEFAULT_MAX_PASSES:
        payload["auto_max_passes"] = cfg.auto_max_passes
    if cfg.context_threshold_percent != DEFAULT_CONTEXT_THRESHOLD_PERCENT:
        payload["context_threshold_percent"] = cfg.context_threshold_percent
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def init_config(
    root: Path, projects_dir: str = DEFAULT_PROJECTS_DIR, force: bool = False
) -> SpecfloConfig:
    path = config_path(root)
    if path.is_file() and not force:
        raise SpecfloError(
            f"Already initialized ({path} exists). Use --force to re-initialize."
        )
    cfg = SpecfloConfig(projects_dir=projects_dir, active_project=None)
    save_config(root, cfg)
    (root / projects_dir).mkdir(parents=True, exist_ok=True)
    return cfg
