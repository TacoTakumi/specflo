"""Plan validation accounts for the requirements of a deferred task.

A requirement or brief that only a deferred task implements is handed to a
follow-up with it. That is not a coverage gap: validation passes and names
the follow-up in a note. A requirement with no task at all still fails.
"""

import re

import pytest
from typer.testing import CliRunner

from specflo import briefs, config, markdown, plan, projects, spec
from specflo.cli import app

runner = CliRunner()

FOLLOWUP = "FU-07"


def _defer(root, cfg, task_id):
    """Write the deferral field into the plan text, as the defer verb does."""
    path = plan.plan_path(root, cfg, "thing")
    path.write_text(markdown.set_entry_field(path.read_text(), task_id, "Deferred to", FOLLOWUP))


def _finish(root, cfg, task_id):
    plan.start_task(root, cfg, "thing", task_id)
    plan.done_task(root, cfg, "thing", task_id)


@pytest.fixture
def project(tmp_path, monkeypatch):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    spec.start_spec(tmp_path, cfg, "thing")
    spec.add_requirement(tmp_path, cfg, "thing", "Prints help", acceptance="exits 0")
    spec.add_requirement(tmp_path, cfg, "thing", "Prints the version", acceptance="exits 0")
    plan.start_plan(tmp_path, cfg, "thing")
    projects.switch_project(tmp_path, cfg, "thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path, cfg


def _add(root, cfg, title, implements, **kwargs):
    return plan.add_task(root, cfg, "thing", title, "a", "uv run pytest",
                         implements=implements, **kwargs).id


def test_a_requirement_of_a_deferred_task_is_noted_not_failed(project):
    root, cfg = project
    _add(root, cfg, "Build help", ["REQ-01"])
    second = _add(root, cfg, "Build version", ["REQ-02"])
    assert plan.validate_plan(root, cfg, "thing") == []
    assert plan.resolution_notes(root, cfg, "thing") == []

    _defer(root, cfg, second)

    assert plan.validate_plan(root, cfg, "thing") == []
    assert plan.resolution_notes(root, cfg, "thing") == [
        f"REQ-02 is deferred to {FOLLOWUP} ({second})"
    ]


def test_validate_prints_the_note_and_passes(project):
    root, cfg = project
    _add(root, cfg, "Build help", ["REQ-01"])
    second = _add(root, cfg, "Build version", ["REQ-02"])
    _defer(root, cfg, second)

    result = runner.invoke(app, ["validate", "plan"])

    assert result.exit_code == 0, result.output
    assert f"REQ-02 is deferred to {FOLLOWUP} ({second})" in result.output


def test_a_requirement_an_active_task_also_implements_gets_no_note(project):
    root, cfg = project
    _add(root, cfg, "Build help", ["REQ-01", "REQ-02"])
    second = _add(root, cfg, "Build version", ["REQ-02"])
    _defer(root, cfg, second)

    assert plan.validate_plan(root, cfg, "thing") == []
    assert plan.resolution_notes(root, cfg, "thing") == []


def test_a_requirement_with_no_task_still_fails(project):
    root, cfg = project
    _add(root, cfg, "Build help", ["REQ-01"])
    second = _add(root, cfg, "Build version", ["REQ-02"])
    path = plan.plan_path(root, cfg, "thing")
    doc = path.read_text()
    path.write_text(doc[: doc.index(f"### {second} ")] + doc[doc.index("## Open questions"):])

    assert "REQ-02 is not implemented by any task." in plan.validate_plan(root, cfg, "thing")


def test_a_brief_only_a_deferred_task_implements_is_noted(project):
    root, cfg = project
    brief_id = briefs.add_brief(root, cfg, "thing", "Colour output").id
    _add(root, cfg, "Build help", ["REQ-01", "REQ-02"])
    second = _add(root, cfg, "Build colours", [brief_id])
    _defer(root, cfg, second)

    assert plan.validate_plan(root, cfg, "thing") == []
    assert plan.resolution_notes(root, cfg, "thing") == [
        f"{brief_id} is deferred to {FOLLOWUP} ({second})"
    ]


def test_a_milestone_of_deferred_tasks_is_not_an_empty_milestone(project):
    root, cfg = project
    one = plan.add_milestone(root, cfg, "thing", "Help works", ["help prints"]).id
    two = plan.add_milestone(root, cfg, "thing", "Version works", ["version prints"]).id
    _add(root, cfg, "Build help", ["REQ-01"], milestone=one)
    second = _add(root, cfg, "Build version", ["REQ-02"], milestone=two)
    assert plan.validate_plan(root, cfg, "thing") == []

    _defer(root, cfg, second)

    assert plan.validate_plan(root, cfg, "thing") == []
    assert plan.resolution_notes(root, cfg, "thing") == [
        f"REQ-02 is deferred to {FOLLOWUP} ({second})"
    ]


def test_a_done_task_may_depend_on_a_deferred_task(project):
    root, cfg = project
    first = _add(root, cfg, "Build help", ["REQ-01", "REQ-02"])
    second = _add(root, cfg, "Build version", ["REQ-02"])
    path = plan.plan_path(root, cfg, "thing")
    path.write_text(markdown.set_entry_field(path.read_text(), first, "Depends on", second))
    path.write_text(markdown.set_entry_field(path.read_text(), first, "Progress", "done"))
    _defer(root, cfg, second)

    assert plan.validate_plan(root, cfg, "thing") == []


def test_an_open_task_that_depends_on_a_deferred_task_fails(project):
    root, cfg = project
    first = _add(root, cfg, "Build help", ["REQ-01", "REQ-02"])
    second = _add(root, cfg, "Build version", ["REQ-02"])
    path = plan.plan_path(root, cfg, "thing")
    path.write_text(markdown.set_entry_field(path.read_text(), first, "Depends on", second))
    _defer(root, cfg, second)

    issues = plan.validate_plan(root, cfg, "thing")

    assert any(re.search(rf"{first} depends on {second}", issue) for issue in issues)
