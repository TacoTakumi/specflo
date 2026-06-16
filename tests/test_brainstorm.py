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


def _bpath(root, cfg, project):
    return brainstorm.brainstorm_path(root, cfg, project)


def test_add_decision_assigns_sequential_ids(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    d1 = brainstorm.add_decision(
        root, cfg, project, "Use SQLite", rationale="simplest", today="2026-06-16"
    )
    d2 = brainstorm.add_decision(
        root, cfg, project, "One file per project", today="2026-06-16"
    )
    assert d1.id == "D-01"
    assert d2.id == "D-02"
    text = _bpath(root, cfg, project).read_text()
    assert "### D-01 — Use SQLite" in text
    assert "- Rationale: simplest" in text
    assert "### D-02 — One file per project" in text
    assert "- Rationale: —" in text  # default when omitted


def test_add_decision_bumps_updated_only(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "Something", today="2026-06-20")
    text = _bpath(root, cfg, project).read_text()
    assert "updated: 2026-06-20" in text
    assert "created: 2026-06-16" in text  # created is unchanged


def test_add_decision_without_start_raises(root, cfg, project):
    with pytest.raises(SpecfloError):
        brainstorm.add_decision(root, cfg, project, "Too early")


def test_decisions_stay_inside_their_section(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "Inside", today="2026-06-16")
    text = _bpath(root, cfg, project).read_text()
    assert text.index("## Decisions") < text.index("### D-01") < text.index("## Out of scope")


def test_supersede_marks_old_and_links_new(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "Use JSON", today="2026-06-16")
    d2 = brainstorm.add_decision(
        root, cfg, project, "Use YAML", rationale="frontmatter", supersedes="D-01",
        today="2026-06-16",
    )
    assert d2.id == "D-02"
    text = _bpath(root, cfg, project).read_text()
    assert "### D-01 — Use JSON" in text          # kept in place
    assert "- Status: superseded by D-02" in text  # old one flipped
    assert "- Supersedes: D-01" in text            # new one links back


def test_supersede_unknown_decision_raises(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    with pytest.raises(SpecfloError):
        brainstorm.add_decision(root, cfg, project, "x", supersedes="D-99")


def test_ids_never_collide_after_supersede(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-16")
    brainstorm.add_decision(root, cfg, project, "a", today="2026-06-16")                       # D-01
    brainstorm.add_decision(root, cfg, project, "b", supersedes="D-01", today="2026-06-16")    # D-02
    d3 = brainstorm.add_decision(root, cfg, project, "c", today="2026-06-16")                  # D-03
    assert d3.id == "D-03"
