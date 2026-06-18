import pytest

from specflo import config, projects, spec
from specflo.errors import SpecfloError


@pytest.fixture
def root(tmp_path):
    config.init_config(tmp_path)
    return tmp_path


@pytest.fixture
def cfg(root):
    return config.load_config(root)


@pytest.fixture
def project(root, cfg):
    """Create a project and return its slug."""
    projects.create_project(root, cfg, "My Thing", created="2026-06-15")
    return "my-thing"


def test_start_creates_spec_with_frontmatter(root, cfg, project):
    path, created = spec.start_spec(root, cfg, project, today="2026-06-18")
    assert created is True
    assert path == root / "docs" / "projects" / "my-thing" / "spec.md"
    text = path.read_text()
    assert "project: my-thing" in text
    assert "phase: spec" in text
    assert "status: draft" in text
    assert "created: 2026-06-18" in text
    assert "updated: 2026-06-18" in text
    assert "# Spec: My Thing" in text
    assert "## Objective" in text
    assert "## Requirements" in text
    assert "### In scope" in text
    assert "### Out of scope" in text
    assert "## Open questions" in text


def test_start_is_idempotent_and_does_not_clobber(root, cfg, project):
    path, created_first = spec.start_spec(root, cfg, project, today="2026-06-18")
    path.write_text(path.read_text() + "\nUSER MARKER\n")
    path_again, created_second = spec.start_spec(root, cfg, project, today="2026-06-19")
    assert created_first is True
    assert created_second is False
    assert path_again == path
    assert "USER MARKER" in path.read_text()  # not overwritten


def test_start_on_missing_project_raises(root, cfg):
    with pytest.raises(SpecfloError):
        spec.start_spec(root, cfg, "ghost")
