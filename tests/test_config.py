import pytest

from specflo import config
from specflo.errors import SpecfloError


def test_init_creates_config_file_and_projects_dir(tmp_path):
    cfg = config.init_config(tmp_path)

    assert (tmp_path / ".specflo" / "config.yaml").is_file()
    assert (tmp_path / "docs" / "projects").is_dir()
    assert cfg.projects_dir == "docs/projects"
    assert cfg.active_project is None


def test_init_respects_a_custom_projects_dir(tmp_path):
    cfg = config.init_config(tmp_path, projects_dir="specs")

    assert (tmp_path / "specs").is_dir()
    assert cfg.projects_dir == "specs"


def test_init_twice_raises_unless_forced(tmp_path):
    config.init_config(tmp_path)

    with pytest.raises(SpecfloError):
        config.init_config(tmp_path)

    # --force re-initializes without error.
    config.init_config(tmp_path, force=True)


def test_save_then_load_round_trips_the_active_project(tmp_path):
    cfg = config.init_config(tmp_path)
    cfg.active_project = "my-thing"
    config.save_config(tmp_path, cfg)

    reloaded = config.load_config(tmp_path)
    assert reloaded.active_project == "my-thing"
    assert reloaded.projects_dir == "docs/projects"


def test_load_without_init_raises(tmp_path):
    with pytest.raises(SpecfloError):
        config.load_config(tmp_path)


def test_find_root_walks_up_from_a_nested_subdir(tmp_path):
    config.init_config(tmp_path)
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    assert config.find_root(nested) == tmp_path


def test_find_root_returns_none_when_not_initialized(tmp_path):
    assert config.find_root(tmp_path) is None
