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


def _good_plan(root, cfg, project):
    """A plan with full bidirectional coverage of a 2-requirement spec."""
    _spec_with_reqs(root, cfg, project, n=2)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "task a", acceptance="a passes", verify="uv run pytest",
                  implements=["REQ-01"], today="2026-06-22")                                  # T-01
    plan.add_task(root, cfg, project, "task b", acceptance="b passes", verify="uv run pytest",
                  implements=["REQ-02"], depends_on=["T-01"], today="2026-06-22")             # T-02


def test_validate_passes_a_complete_plan(root, cfg, project):
    _good_plan(root, cfg, project)
    assert plan.validate_plan(root, cfg, project) == []


def test_validate_flags_missing_file(root, cfg, project):
    assert any("not found" in i for i in plan.validate_plan(root, cfg, project))


def test_validate_flags_no_tasks(root, cfg, project):
    _spec_with_reqs(root, cfg, project)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    assert any("no tasks" in i for i in plan.validate_plan(root, cfg, project))


def test_validate_flags_uncovered_requirement(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=2)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "only a", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")  # REQ-02 left uncovered
    issues = plan.validate_plan(root, cfg, project)
    assert any("REQ-02" in i and "not implemented" in i for i in issues)


def test_validate_flags_missing_acceptance_and_verify(root, cfg, project):
    _good_plan(root, cfg, project)
    path = _ppath(root, cfg, project)
    path.write_text(path.read_text()
                    .replace("- Acceptance: a passes", "- Acceptance: ")
                    .replace("- Verify: uv run pytest", "- Verify: ", 1))
    issues = plan.validate_plan(root, cfg, project)
    assert any("acceptance" in i.lower() for i in issues)
    assert any("verification" in i.lower() for i in issues)


def test_validate_flags_dangling_dependency_and_cycle(root, cfg, project):
    _good_plan(root, cfg, project)
    path = _ppath(root, cfg, project)
    # make T-01 depend on T-02 -> cycle (T-01 -> T-02 -> T-01)
    text = path.read_text().replace(
        "### T-01 — task a\n- Acceptance: a passes\n- Verify: uv run pytest\n- Implements: REQ-01\n",
        "### T-01 — task a\n- Acceptance: a passes\n- Verify: uv run pytest\n- Implements: REQ-01\n- Depends on: T-02\n",
    )
    path.write_text(text)
    assert any("cycle" in i for i in plan.validate_plan(root, cfg, project))


def test_validate_ignores_superseded_tasks(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "old", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")                                  # T-01
    plan.add_task(root, cfg, project, "new", acceptance="b", verify="v",
                  implements=["REQ-01"], supersedes="T-01", today="2026-06-22")               # T-02
    # blank the superseded entry's acceptance — validate must still pass
    path = _ppath(root, cfg, project)
    path.write_text(path.read_text().replace("- Acceptance: a", "- Acceptance: "))
    assert plan.validate_plan(root, cfg, project) == []


def test_plan_warnings_flags_scope_reduction_vocab(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "ship a stub for now",
                  acceptance="returns a value", verify="v",
                  implements=["REQ-01"], today="2026-06-22")
    warnings = plan.plan_warnings(root, cfg, project)
    assert any("stub" in w for w in warnings)
    assert any("for now" in w for w in warnings)


def test_plan_warnings_empty_on_clean_plan(root, cfg, project):
    _good_plan(root, cfg, project)
    assert plan.plan_warnings(root, cfg, project) == []


def test_complete_plan_flips_status_and_leaves_tasks_untouched(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "t", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")
    plan.complete_plan(root, cfg, project, today="2026-06-24")
    text = _ppath(root, cfg, project).read_text()
    assert "status: complete" in text
    assert "status: draft" not in text
    assert "updated: 2026-06-24" in text
    assert "- Status: active" in text   # task entry untouched


def test_complete_plan_without_file_raises(root, cfg, project):
    with pytest.raises(SpecfloError):
        plan.complete_plan(root, cfg, project)


def test_progress_transitions(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "t", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")  # T-01
    plan.start_task(root, cfg, project, "T-01")
    assert "- Progress: in_progress" in _ppath(root, cfg, project).read_text()
    plan.done_task(root, cfg, project, "T-01")
    assert "- Progress: done" in _ppath(root, cfg, project).read_text()
    plan.block_task(root, cfg, project, "T-01", reason="waiting on API")
    text = _ppath(root, cfg, project).read_text()
    assert "- Progress: blocked" in text
    assert "- Blocked: waiting on API" in text
    plan.reopen_task(root, cfg, project, "T-01")
    text = _ppath(root, cfg, project).read_text()
    assert "- Progress: pending" in text
    assert "Blocked" not in text  # cleared on reopen


def test_done_requires_in_progress(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "t", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")   # T-01 pending
    with pytest.raises(SpecfloError):
        plan.done_task(root, cfg, project, "T-01")             # pending -> done refused
    plan.start_task(root, cfg, project, "T-01")
    plan.done_task(root, cfg, project, "T-01")                 # in_progress -> done ok
    assert "- Progress: done" in _ppath(root, cfg, project).read_text()


def test_transition_on_unknown_or_superseded_task_raises(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "old", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")                                # T-01
    plan.add_task(root, cfg, project, "new", acceptance="b", verify="v",
                  implements=["REQ-01"], supersedes="T-01", today="2026-06-22")             # T-02
    with pytest.raises(SpecfloError):
        plan.start_task(root, cfg, project, "T-99")
    with pytest.raises(SpecfloError):
        plan.start_task(root, cfg, project, "T-01")  # superseded -> frozen


def test_plan_progress_is_dependency_aware(root, cfg, project):
    _good_plan(root, cfg, project)  # T-01, T-02 (T-02 depends on T-01)
    prog = plan.plan_progress(root, cfg, project)
    assert prog["total"] == 2
    assert prog["by_state"]["pending"] == 2
    assert prog["next_actionable"] == ["T-01"]  # T-02 blocked by its dep
    assert prog["all_done"] is False
    plan.start_task(root, cfg, project, "T-01")
    plan.done_task(root, cfg, project, "T-01")
    prog = plan.plan_progress(root, cfg, project)
    assert prog["done"] == 1
    assert prog["next_actionable"] == ["T-02"]  # dep now satisfied
    plan.start_task(root, cfg, project, "T-02")
    plan.done_task(root, cfg, project, "T-02")
    assert plan.plan_progress(root, cfg, project)["all_done"] is True


def test_plan_progress_zero_when_no_plan(root, cfg, project):
    prog = plan.plan_progress(root, cfg, project)
    assert prog["total"] == 0 and prog["all_done"] is False


def test_list_tasks_hides_superseded_by_default(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "old", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")                                # T-01
    plan.add_task(root, cfg, project, "new", acceptance="b", verify="v",
                  implements=["REQ-01"], supersedes="T-01", today="2026-06-22")             # T-02
    assert [t.id for t in plan.list_tasks(root, cfg, project)] == ["T-02"]
    assert [t.id for t in plan.list_tasks(root, cfg, project, include_superseded=True)] == ["T-01", "T-02"]


def test_task_brief_assembles_task_reqs_and_constraints(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=2)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    ppath = _ppath(root, cfg, project)
    ppath.write_text(ppath.read_text().replace(
        "## Global constraints\n"
        "<!-- optional; project-wide invariants copied verbatim from the spec,"
        " implicitly part of every task -->",
        "## Global constraints\n- Python 3.12; use uv."))
    plan.add_task(root, cfg, project, "build a", acceptance="a works",
                  verify="uv run pytest", implements=["REQ-01"], today="2026-06-22")  # T-01
    brief = plan.task_brief(root, cfg, project, "T-01")
    assert brief["task"]["id"] == "T-01"
    assert brief["task"]["acceptance"] == "a works"
    assert brief["requirements"][0]["id"] == "REQ-01"
    assert "REQ-01" in brief["requirements"][0]["section"]
    assert "Python 3.12" in brief["global_constraints"]


def test_task_brief_defaults_to_first_next_actionable(root, cfg, project):
    _good_plan(root, cfg, project)   # T-01, then T-02 (depends on T-01)
    brief = plan.task_brief(root, cfg, project)   # no id -> first actionable
    assert brief["task"]["id"] == "T-01"          # T-02 blocked by T-01


def test_task_brief_unknown_task_raises(root, cfg, project):
    _good_plan(root, cfg, project)
    with pytest.raises(SpecfloError):
        plan.task_brief(root, cfg, project, "T-99")
