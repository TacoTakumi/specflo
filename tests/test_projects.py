import pytest

from specflo import config, projects
from specflo.errors import SpecfloError


@pytest.fixture
def root(tmp_path):
    config.init_config(tmp_path)
    return tmp_path


@pytest.fixture
def cfg(root):
    return config.load_config(root)


def test_slugify_normalizes_names():
    assert projects.slugify("My Cool Thing") == "my-cool-thing"
    assert projects.slugify("Hello, World!") == "hello-world"
    assert projects.slugify("  spaced__out  ") == "spaced-out"


def test_slugify_rejects_names_with_no_usable_characters():
    with pytest.raises(SpecfloError):
        projects.slugify("!!!")


def test_create_project_writes_artifact_and_returns_project(root, cfg):
    project = projects.create_project(root, cfg, "My Thing", created="2026-06-15")

    project_md = root / "docs" / "projects" / "my-thing" / "project.md"
    assert project_md.is_file()
    assert project.name == "My Thing"
    assert project.slug == "my-thing"
    assert project.phase == "brainstorm"
    assert project.status == "active"
    assert project.created == "2026-06-15"


def test_creating_a_duplicate_project_raises(root, cfg):
    projects.create_project(root, cfg, "My Thing")
    with pytest.raises(SpecfloError):
        projects.create_project(root, cfg, "My Thing")


def test_load_project_reads_back_the_saved_fields(root, cfg):
    projects.create_project(root, cfg, "My Thing", created="2026-06-15")

    loaded = projects.load_project(root, cfg, "my-thing")
    assert loaded.name == "My Thing"
    assert loaded.slug == "my-thing"
    assert loaded.phase == "brainstorm"
    assert loaded.status == "active"
    assert loaded.created == "2026-06-15"


def test_load_missing_project_raises(root, cfg):
    with pytest.raises(SpecfloError):
        projects.load_project(root, cfg, "does-not-exist")
