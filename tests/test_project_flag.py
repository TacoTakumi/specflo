"""The project override: a command names its project without moving the
stored active-project pointer."""

import re
from pathlib import Path

import pytest
import yaml

from specflo import config, projects

SRC = Path(__file__).resolve().parent.parent / "src" / "specflo"


def _stored(root: Path) -> str | None:
    return yaml.safe_load(config.config_path(root).read_text()).get("active_project")


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A checkout with projects a (stored active) and b; the override is reset after."""
    monkeypatch.setattr(config, "_project_override", None)
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "a")
    projects.create_project(tmp_path, cfg, "b")
    config.set_active_project(cfg, "a")
    config.save_config(tmp_path, cfg)
    return tmp_path


def test_stored_pointer_survives_a_save_under_an_override(checkout):
    config.set_project_override("b")
    cfg = config.load_config(checkout)
    assert cfg.active_project == "b"

    config.save_config(checkout, cfg)

    assert _stored(checkout) == "a"


def test_stored_pointer_moves_through_the_helper_under_an_override(checkout):
    config.set_project_override("b")
    cfg = config.load_config(checkout)

    config.set_active_project(cfg, "b")
    config.save_config(checkout, cfg)
    assert _stored(checkout) == "b"

    cfg = config.load_config(checkout)
    config.set_active_project(cfg, None)
    config.save_config(checkout, cfg)
    assert _stored(checkout) is None


def test_stored_pointer_is_the_loaded_value_without_an_override(checkout):
    cfg = config.load_config(checkout)

    assert cfg.active_project == "a"
    assert cfg.project_override is None
    cfg.active_project = "b"
    config.save_config(checkout, cfg)
    assert _stored(checkout) == "b"


def test_stored_pointer_moves_only_through_the_helper():
    """Outside config.py no module assigns the pointer directly: under an
    override such an assignment would not be saved."""
    assignment = re.compile(r"\.active_project\s*=(?!=)")
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        if path.name != "config.py" or path.parent != SRC
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if assignment.search(line)
    ]
    assert offenders == []
