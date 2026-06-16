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


@dataclass
class SpecfloConfig:
    projects_dir: str = DEFAULT_PROJECTS_DIR
    active_project: str | None = None


def config_path(root: Path) -> Path:
    return root / CONFIG_DIRNAME / CONFIG_FILENAME


def find_root(start: Path) -> Path | None:
    """Walk up from ``start`` to the first directory holding a specflo config."""
    start = Path(start).resolve()
    for directory in (start, *start.parents):
        if config_path(directory).is_file():
            return directory
    return None


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
    )


def save_config(root: Path, cfg: SpecfloConfig) -> None:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"projects_dir": cfg.projects_dir, "active_project": cfg.active_project}
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
