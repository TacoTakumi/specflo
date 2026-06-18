import pytest

from specflo import brainstorm, config, projects, spec
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


def _spath(root, cfg, project):
    return spec.spec_path(root, cfg, project)


def test_add_requirement_assigns_sequential_ids(root, cfg, project):
    spec.start_spec(root, cfg, project, today="2026-06-18")
    r1 = spec.add_requirement(
        root, cfg, project, "CLI prints help on no args",
        acceptance="`specflo` with no args exits 0 and prints usage", today="2026-06-18",
    )
    r2 = spec.add_requirement(
        root, cfg, project, "validate spec reports issues",
        acceptance="exit 1 + an issue list when a requirement lacks acceptance", today="2026-06-18",
    )
    assert r1.id == "REQ-01"
    assert r2.id == "REQ-02"
    text = _spath(root, cfg, project).read_text()
    assert "### REQ-01 — CLI prints help on no args" in text
    assert "- Acceptance: `specflo` with no args exits 0 and prints usage" in text
    assert "### REQ-02 — validate spec reports issues" in text
    assert "- Status: active" in text


def test_add_requirement_bumps_updated_only(root, cfg, project):
    spec.start_spec(root, cfg, project, today="2026-06-18")
    spec.add_requirement(root, cfg, project, "x", acceptance="y", today="2026-06-20")
    text = _spath(root, cfg, project).read_text()
    assert "updated: 2026-06-20" in text
    assert "created: 2026-06-18" in text  # created is unchanged


def test_add_requirement_without_start_raises(root, cfg, project):
    with pytest.raises(SpecfloError):
        spec.add_requirement(root, cfg, project, "Too early", acceptance="never")


def test_requirements_stay_inside_their_section(root, cfg, project):
    spec.start_spec(root, cfg, project, today="2026-06-18")
    spec.add_requirement(root, cfg, project, "Inside", acceptance="a", today="2026-06-18")
    text = _spath(root, cfg, project).read_text()
    assert text.index("## Requirements") < text.index("### REQ-01") < text.index("## Boundaries")


def test_supersede_marks_old_and_links_new(root, cfg, project):
    spec.start_spec(root, cfg, project, today="2026-06-18")
    spec.add_requirement(root, cfg, project, "old", acceptance="a", today="2026-06-18")
    r2 = spec.add_requirement(
        root, cfg, project, "new", acceptance="b", supersedes="REQ-01", today="2026-06-18",
    )
    assert r2.id == "REQ-02"
    assert r2.supersedes == "REQ-01"
    text = _spath(root, cfg, project).read_text()
    assert "### REQ-01 — old" in text                 # kept in place
    assert "- Status: superseded by REQ-02" in text   # old one flipped
    assert "- Supersedes: REQ-01" in text             # new one links back


def test_supersede_unknown_requirement_raises(root, cfg, project):
    spec.start_spec(root, cfg, project, today="2026-06-18")
    with pytest.raises(SpecfloError):
        spec.add_requirement(root, cfg, project, "x", acceptance="a", supersedes="REQ-99")


def test_ids_never_collide_after_supersede(root, cfg, project):
    spec.start_spec(root, cfg, project, today="2026-06-18")
    spec.add_requirement(root, cfg, project, "a", acceptance="a", today="2026-06-18")                    # REQ-01
    spec.add_requirement(root, cfg, project, "b", acceptance="b", supersedes="REQ-01", today="2026-06-18")  # REQ-02
    r3 = spec.add_requirement(root, cfg, project, "c", acceptance="c", today="2026-06-18")               # REQ-03
    assert r3.id == "REQ-03"


def test_from_records_the_link_when_decision_exists(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-18")
    brainstorm.add_decision(root, cfg, project, "Use SQLite", today="2026-06-18")  # D-01
    spec.start_spec(root, cfg, project, today="2026-06-18")
    r = spec.add_requirement(
        root, cfg, project, "Store data in SQLite",
        acceptance="data survives a restart", derives_from="D-01", today="2026-06-18",
    )
    assert r.derives_from == "D-01"
    assert "- Derives from: D-01" in _spath(root, cfg, project).read_text()


def test_from_unknown_decision_raises(root, cfg, project):
    brainstorm.start_brainstorm(root, cfg, project, today="2026-06-18")
    spec.start_spec(root, cfg, project, today="2026-06-18")
    with pytest.raises(SpecfloError):
        spec.add_requirement(
            root, cfg, project, "x", acceptance="a", derives_from="D-99",
        )


def test_from_without_a_brainstorm_raises(root, cfg, project):
    spec.start_spec(root, cfg, project, today="2026-06-18")  # no brainstorm.md created
    with pytest.raises(SpecfloError):
        spec.add_requirement(
            root, cfg, project, "x", acceptance="a", derives_from="D-01",
        )
