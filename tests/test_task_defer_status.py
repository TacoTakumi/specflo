"""A task that carries a `Deferred to` field is deferred.

A deferred task is work the project hands to a follow-up. It is not open work:
the end-of-execute check, the next-task choice and the progress counts leave
it out. Its entry is frozen: only a note can still be appended, and no other
task can depend on it or supersede it.
"""

import pytest
from typer.testing import CliRunner

from specflo import config, markdown, plan, projects, spec
from specflo.cli import app
from specflo.errors import SpecfloError

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
def planned(tmp_path, monkeypatch):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    spec.start_spec(tmp_path, cfg, "thing")
    spec.add_requirement(tmp_path, cfg, "thing", "Prints help", acceptance="exits 0")
    plan.start_plan(tmp_path, cfg, "thing")
    ids = [
        plan.add_task(tmp_path, cfg, "thing", title, "a", "uv run pytest",
                      implements=["REQ-01"]).id
        for title in ("Build the parser", "Build the printer", "Build the linter")
    ]
    projects.switch_project(tmp_path, cfg, "thing")
    record = projects.project_dir(tmp_path, cfg, "thing") / projects.PROJECT_FILENAME
    record.write_text(record.read_text().replace("phase: brainstorm", "phase: execute"))
    monkeypatch.chdir(tmp_path)
    return tmp_path, cfg, ids


def _task(root, cfg, task_id):
    return next(
        t for t in plan.list_tasks(root, cfg, "thing", include_superseded=True)
        if t.id == task_id
    )


def test_the_field_gives_the_task_the_deferred_status(planned):
    root, cfg, (first, second, third) = planned
    _defer(root, cfg, second)

    task = _task(root, cfg, second)

    assert task.status == "deferred"
    assert task.deferred_to == FOLLOWUP
    assert _task(root, cfg, first).status == "active"
    assert _task(root, cfg, first).deferred_to is None


def test_a_deferred_task_does_not_hold_the_end_of_execute(planned):
    root, cfg, (first, second, third) = planned
    _finish(root, cfg, first)
    _finish(root, cfg, third)
    _defer(root, cfg, second)

    assert plan.reconcile_issues(root, cfg, "thing") == []
    assert plan.current_task_id(root, cfg, "thing") is None
    assert plan.plan_progress(root, cfg, "thing")["next_actionable"] == []
    assert "Tasks:   2/2 done" in runner.invoke(app, ["status"]).output


def test_a_pending_task_is_still_reported_as_open(planned):
    root, cfg, (first, second, third) = planned
    _finish(root, cfg, first)
    _defer(root, cfg, second)

    assert plan.current_task_id(root, cfg, "thing") == third
    issues = plan.reconcile_issues(root, cfg, "thing")
    assert len(issues) == 1
    assert third in issues[0] and second not in issues[0]


def test_the_checkpoint_does_not_send_the_agent_to_the_deferred_task(planned):
    root, cfg, (first, second, third) = planned
    _finish(root, cfg, first)
    _defer(root, cfg, second)

    result = runner.invoke(app, ["checkpoint"])

    assert result.exit_code == 0, result.output
    assert third in result.output
    assert second not in result.output


@pytest.mark.parametrize("verb", [
    lambda r, c, t: plan.edit_task(r, c, "thing", t, title="Another title"),
    lambda r, c, t: plan.edit_task(r, c, "thing", t, title="Another title", force=True),
    lambda r, c, t: plan.start_task(r, c, "thing", t),
    lambda r, c, t: plan.done_task(r, c, "thing", t),
    lambda r, c, t: plan.block_task(r, c, "thing", t, reason="waiting"),
    lambda r, c, t: plan.reopen_task(r, c, "thing", t),
    lambda r, c, t: plan.set_milestone(r, c, "thing", t, "M-01"),
], ids=["edit", "edit-force", "start", "done", "block", "reopen", "set-milestone"])
def test_a_deferred_entry_is_frozen(planned, verb):
    root, cfg, (first, second, third) = planned
    _defer(root, cfg, second)
    path = plan.plan_path(root, cfg, "thing")
    before = path.read_bytes()

    with pytest.raises(SpecfloError) as refusal:
        verb(root, cfg, second)

    assert f"deferred to {FOLLOWUP}" in str(refusal.value)
    assert path.read_bytes() == before


def test_a_note_still_appends_to_a_deferred_entry(planned):
    root, cfg, (first, second, third) = planned
    _defer(root, cfg, second)

    plan.add_note(root, cfg, "thing", second, "the vendor withdrew the format")

    assert _task(root, cfg, second).notes[-1]["text"] == "the vendor withdrew the format"
    assert _task(root, cfg, second).status == "deferred"


def test_no_task_can_depend_on_or_supersede_a_deferred_task(planned):
    root, cfg, (first, second, third) = planned
    _defer(root, cfg, second)
    path = plan.plan_path(root, cfg, "thing")
    before = path.read_bytes()

    with pytest.raises(SpecfloError, match=f"deferred to {FOLLOWUP}"):
        plan.add_task(root, cfg, "thing", "Ship it", "a", "uv run pytest",
                      implements=["REQ-01"], depends_on=[second])
    with pytest.raises(SpecfloError, match=f"deferred to {FOLLOWUP}"):
        plan.edit_task(root, cfg, "thing", third, add_depends_on=[second])
    with pytest.raises(SpecfloError, match=f"deferred to {FOLLOWUP}"):
        plan.add_task(root, cfg, "thing", "Build the printer again", "a", "uv run pytest",
                      implements=["REQ-01"], supersedes=second)

    assert path.read_bytes() == before


def test_the_cli_refuses_a_verb_on_a_deferred_task(planned):
    root, cfg, (first, second, third) = planned
    _defer(root, cfg, second)

    result = runner.invoke(app, ["task", "start", second])

    assert result.exit_code != 0
    assert f"deferred to {FOLLOWUP}" in result.output


def test_a_superseded_task_keeps_its_own_refusal(planned):
    root, cfg, (first, second, third) = planned
    plan.add_task(root, cfg, "thing", "Build the printer again", "a", "uv run pytest",
                  implements=["REQ-01"], supersedes=second)

    with pytest.raises(SpecfloError, match="superseded"):
        plan.start_task(root, cfg, "thing", second)
