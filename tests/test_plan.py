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


def _spec_with_reqs(root, cfg, project, n=2):
    spec.start_spec(root, cfg, project, today="2026-06-22")
    for i in range(n):
        spec.add_requirement(root, cfg, project, f"req {i}", acceptance="ok", today="2026-06-22")


def test_add_task_assigns_sequential_ids_and_records_fields(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=2)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    t1 = plan.add_task(root, cfg, project, "build the parser",
                       acceptance="parses a task block", verify="uv run pytest",
                       implements=["REQ-01"], today="2026-06-22")
    t2 = plan.add_task(root, cfg, project, "wire the CLI",
                       acceptance="command exits 0", verify="uv run specflo task list",
                       implements=["REQ-01", "REQ-02"], depends_on=["T-01"], today="2026-06-22")
    assert t1.id == "T-01" and t2.id == "T-02"
    text = _ppath(root, cfg, project).read_text()
    assert "### T-01 — build the parser" in text
    assert "- Acceptance: parses a task block" in text
    assert "- Verify: uv run pytest" in text
    assert "- Implements: REQ-01, REQ-02" in text
    assert "- Depends on: T-01" in text
    assert "- Progress: pending" in text
    assert "- Status: active" in text


def test_add_task_requires_at_least_one_requirement(root, cfg, project):
    _spec_with_reqs(root, cfg, project)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    with pytest.raises(SpecfloError):
        plan.add_task(root, cfg, project, "orphan", acceptance="a", verify="v", implements=[])


def test_add_task_rejects_unknown_or_superseded_requirement(root, cfg, project):
    spec.start_spec(root, cfg, project, today="2026-06-22")
    spec.add_requirement(root, cfg, project, "old", acceptance="a", today="2026-06-22")          # REQ-01
    spec.add_requirement(root, cfg, project, "new", acceptance="b",
                         supersedes="REQ-01", today="2026-06-22")                                  # REQ-02
    plan.start_plan(root, cfg, project, today="2026-06-22")
    with pytest.raises(SpecfloError):
        plan.add_task(root, cfg, project, "x", acceptance="a", verify="v", implements=["REQ-99"])
    with pytest.raises(SpecfloError):  # superseded REQ is not active
        plan.add_task(root, cfg, project, "x", acceptance="a", verify="v", implements=["REQ-01"])


def test_add_task_validates_dependency_and_supersede_targets(root, cfg, project):
    _spec_with_reqs(root, cfg, project)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "first", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")                                       # T-01
    with pytest.raises(SpecfloError):
        plan.add_task(root, cfg, project, "bad dep", acceptance="a", verify="v",
                      implements=["REQ-01"], depends_on=["T-99"])
    with pytest.raises(SpecfloError):
        plan.add_task(root, cfg, project, "bad sup", acceptance="a", verify="v",
                      implements=["REQ-01"], supersedes="T-99")
    t = plan.add_task(root, cfg, project, "replacement", acceptance="a", verify="v",
                      implements=["REQ-01"], supersedes="T-01", today="2026-06-22")                # T-02
    text = _ppath(root, cfg, project).read_text()
    assert t.id == "T-02"
    assert "- Status: superseded by T-02" in text
    assert "- Supersedes: T-01" in text


def test_add_task_without_start_raises(root, cfg, project):
    with pytest.raises(SpecfloError):
        plan.add_task(root, cfg, project, "early", acceptance="a", verify="v", implements=["REQ-01"])
