import pytest

from specflo import brainstorm, config, projects
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


def test_start_creates_brainstorm_with_frontmatter(root, cfg, project):
    path, created = brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    assert created is True
    assert path == root / "docs" / "projects" / "my-thing" / "brainstorm.md"
    text = path.read_text()
    assert "project: my-thing" in text
    assert "phase: brainstorm" in text
    assert "status: draft" in text
    assert "created: 2026-06-16" in text
    assert "updated: 2026-06-16" in text
    assert "# Brainstorm: My Thing" in text
    assert "## Decisions" in text
    assert "## Out of scope / Deferred" in text
    assert "## Open questions" in text


def test_start_is_idempotent_and_does_not_clobber(root, cfg, project):
    path, created_first = brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    path.write_text(path.read_text() + "\nUSER MARKER\n")
    path_again, created_second = brainstorm.start_brainstorm(root, cfg, project, today="2026-06-17")
    assert created_first is True
    assert created_second is False
    assert path_again == path
    assert "USER MARKER" in path.read_text()  # not overwritten


def test_start_on_missing_project_raises(root, cfg):
    with pytest.raises(SpecfloError):
        brainstorm.start_brainstorm(root, cfg, "ghost")
