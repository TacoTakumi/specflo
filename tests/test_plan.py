import pytest

from specflo import brainstorm, config, plan, projects, spec
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
    projects.create_project(root, cfg, "My Thing", created="2026-06-15")
    return "my-thing"


def _ppath(root, cfg, project):
    return plan.plan_path(root, cfg, project)


def test_start_creates_plan_with_frontmatter(root, cfg, project):
    path, created = plan.start_plan(root, cfg, project, today="2026-06-22")
    assert created is True
    assert path == root / "docs" / "projects" / "my-thing" / "plan.md"
    text = path.read_text()
    assert "project: my-thing" in text
    assert "phase: plan" in text
    assert "status: draft" in text
    assert "created: 2026-06-22" in text
    assert "# Plan: My Thing" in text
    assert "## Approach" in text
    assert "## Tasks" in text
    assert "## Open questions" in text


def test_start_is_idempotent_and_does_not_clobber(root, cfg, project):
    path, first = plan.start_plan(root, cfg, project, today="2026-06-22")
    path.write_text(path.read_text() + "\nUSER MARKER\n")
    path_again, second = plan.start_plan(root, cfg, project, today="2026-06-23")
    assert first is True and second is False
    assert path_again == path
    assert "USER MARKER" in path.read_text()


def test_start_on_missing_project_raises(root, cfg):
    with pytest.raises(SpecfloError):
        plan.start_plan(root, cfg, "ghost")
