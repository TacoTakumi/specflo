"""`task defer` refuses a task it must not defer, and then changes nothing.

Work that started or ended, a task other open work rests on, and the last
active task of a plan all stay where they are. A refusal writes neither
document and uses no follow-up number.
"""

import pytest
from typer.testing import CliRunner

from specflo import config, followup, markdown, plan, projects, spec
from specflo.cli import app

runner = CliRunner()


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
    # An earlier follow-up, so the document exists and a number is in use.
    followup.add_followup(tmp_path, cfg, "thing", "Flaky test", "Fix it")
    monkeypatch.chdir(tmp_path)
    return tmp_path, cfg, ids


def _documents(root, cfg):
    return (
        plan.plan_path(root, cfg, "thing").read_bytes(),
        followup.followup_path(root, cfg, "thing").read_bytes(),
    )


def _defer(task_id):
    return runner.invoke(app, ["task", "defer", task_id, "--do", "X", "--why", "Y"])


def _refused(root, cfg, task_id, *expected):
    """Defer is refused with every expected text, and nothing changed."""
    before = _documents(root, cfg)
    result = _defer(task_id)
    assert result.exit_code != 0, result.output
    for text in expected:
        assert text in result.output
    assert _documents(root, cfg) == before
    return result


def test_a_done_task_is_refused(planned):
    root, cfg, (first, second, third) = planned
    plan.start_task(root, cfg, "thing", second)
    plan.done_task(root, cfg, "thing", second)

    _refused(root, cfg, second, "is done")


def test_a_task_in_progress_is_refused_and_names_reopen(planned):
    root, cfg, (first, second, third) = planned
    plan.start_task(root, cfg, "thing", second)

    _refused(root, cfg, second, "in_progress", f"specflo task reopen {second}")


def test_a_superseded_task_is_refused(planned):
    root, cfg, (first, second, third) = planned
    plan.add_task(root, cfg, "thing", "Build the printer again", "a", "uv run pytest",
                  implements=["REQ-01"], supersedes=second)

    _refused(root, cfg, second, "superseded")


def test_a_deferred_task_is_refused(planned):
    root, cfg, (first, second, third) = planned
    assert _defer(second).exit_code == 0
    entry = followup.list_followups(root, cfg)[-1]

    _refused(root, cfg, second, f"deferred to {entry.id}")


def test_an_unknown_task_is_refused(planned):
    root, cfg, _ = planned

    _refused(root, cfg, "T-99", "No task T-99")


def test_a_refusal_uses_no_followup_number(planned):
    root, cfg, (first, second, third) = planned
    plan.start_task(root, cfg, "thing", second)
    for task_id in (second, "T-99"):
        _refused(root, cfg, task_id)

    entry = followup.add_followup(root, cfg, "thing", "Another", "Do it")

    assert entry.id == "FU-02"


def test_a_task_open_work_depends_on_is_refused(planned):
    root, cfg, (first, second, third) = planned
    plan.edit_task(root, cfg, "thing", third, add_depends_on=[second])
    plan.edit_task(root, cfg, "thing", first, add_depends_on=[second])

    _refused(root, cfg, second, first, third, "specflo task rewire")


def test_a_blocked_dependent_also_refuses(planned):
    root, cfg, (first, second, third) = planned
    plan.edit_task(root, cfg, "thing", third, add_depends_on=[second])
    plan.block_task(root, cfg, "thing", third, reason="waiting")

    _refused(root, cfg, second, third)


def test_a_done_dependent_does_not_refuse(planned):
    root, cfg, (first, second, third) = planned
    path = plan.plan_path(root, cfg, "thing")
    doc = markdown.set_entry_field(path.read_text(), third, "Depends on", second)
    path.write_text(markdown.set_entry_field(doc, third, "Progress", "done"))

    result = _defer(second)

    assert result.exit_code == 0, result.output
    assert plan.validate_plan(root, cfg, "thing") == []


def test_the_only_active_task_is_refused_and_names_shelve(planned):
    root, cfg, (first, second, third) = planned
    assert _defer(first).exit_code == 0
    assert _defer(second).exit_code == 0

    _refused(root, cfg, third, "only active task", "specflo shelve")


def test_a_project_with_no_plan_is_refused(tmp_path, monkeypatch):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    projects.switch_project(tmp_path, cfg, "thing")
    monkeypatch.chdir(tmp_path)

    result = _defer("T-01")

    assert result.exit_code != 0
    assert "No plan yet" in result.output
    assert not followup.followup_path(tmp_path, cfg, "thing").is_file()


def test_a_task_open_work_reaches_through_a_supersession_is_refused(planned):
    root, cfg, (first, second, third) = planned
    plan.edit_task(root, cfg, "thing", third, add_depends_on=[second])
    newer = plan.add_task(root, cfg, "thing", "Build the printer again", "a",
                          "uv run pytest", implements=["REQ-01"], supersedes=second).id
    newest = plan.add_task(root, cfg, "thing", "Build the printer once more", "a",
                           "uv run pytest", implements=["REQ-01"], supersedes=newer).id

    _refused(root, cfg, newest, third, f"--drop-depends-on {second}")


def test_a_replacement_nothing_waits_on_can_be_deferred(planned):
    root, cfg, (first, second, third) = planned
    newer = plan.add_task(root, cfg, "thing", "Build the printer again", "a",
                          "uv run pytest", implements=["REQ-01"], supersedes=second).id

    assert _defer(newer).exit_code == 0
