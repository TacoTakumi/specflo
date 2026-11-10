"""`task defer` hands a task the project will not do to a follow-up.

One command adds the follow-up, marks the task as deferred to it and records
the reason as the last note of the task. The task content stays in the plan.
"""

import datetime
import json

import pytest
from typer.testing import CliRunner

from specflo import config, followup, plan, projects, spec
from specflo.cli import app

runner = CliRunner()

TODAY = datetime.date.today().isoformat()


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
    monkeypatch.chdir(tmp_path)
    return tmp_path, cfg, ids


def _task(root, cfg, task_id):
    return next(
        t for t in plan.list_tasks(root, cfg, "thing", include_superseded=True)
        if t.id == task_id
    )


def _documents(root, cfg):
    path = followup.followup_path(root, cfg, "thing")
    return (
        plan.plan_path(root, cfg, "thing").read_bytes(),
        path.read_bytes() if path.is_file() else None,
    )


def test_defer_adds_the_followup_and_marks_the_task(planned):
    root, cfg, (first, second, third) = planned

    result = runner.invoke(app, [
        "task", "defer", second, "--do", "Build the printer for the new format",
        "--why", "The vendor withdrew the format",
    ])

    assert result.exit_code == 0, result.output
    [entry] = followup.list_followups(root, cfg)
    assert entry.title == "Build the printer"
    assert entry.do == "Build the printer for the new format"
    assert entry.source == f"{second} (deferred)"
    assert entry.status == "open"
    assert entry.project == "thing"
    task = _task(root, cfg, second)
    assert task.status == "deferred"
    assert task.deferred_to == entry.id
    assert second in result.output and entry.id in result.output


def test_the_reason_is_the_last_note_of_the_task(planned):
    root, cfg, (first, second, third) = planned
    plan.add_note(root, cfg, "thing", second, "the format has two variants")

    runner.invoke(app, ["task", "defer", second, "--do", "X", "--why", "The vendor withdrew it"])

    notes = _task(root, cfg, second).notes
    assert [n["text"] for n in notes] == ["the format has two variants", "The vendor withdrew it"]
    assert notes[-1] == {"date": TODAY, "label": "Deferred", "text": "The vendor withdrew it"}


def test_a_blocked_task_can_be_deferred(planned):
    root, cfg, (first, second, third) = planned
    plan.block_task(root, cfg, "thing", second, reason="waiting on the vendor")

    result = runner.invoke(app, ["task", "defer", second, "--do", "X", "--why", "Y"])

    assert result.exit_code == 0, result.output
    assert _task(root, cfg, second).status == "deferred"


def test_json_names_the_task_and_the_followup(planned):
    root, cfg, (first, second, third) = planned

    result = runner.invoke(app, ["task", "defer", second, "--do", "X", "--why", "Y", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    [entry] = followup.list_followups(root, cfg)
    assert payload["task"] == second
    assert payload["followup"] == entry.id


def test_the_followup_takes_the_next_number_of_the_checkout(planned):
    root, cfg, (first, second, third) = planned
    earlier = followup.add_followup(root, cfg, "thing", "Flaky test", "Fix it")

    runner.invoke(app, ["task", "defer", second, "--do", "X", "--why", "Y"])

    number = int(earlier.id.split("-")[1]) + 1
    assert _task(root, cfg, second).deferred_to == f"FU-{number:02d}"


@pytest.mark.parametrize("arguments", [
    ["--do", "X"],
    ["--why", "Y"],
    ["--do", "X", "--why", "  "],
    ["--do", " ", "--why", "Y"],
    ["--do", "one\ntwo", "--why", "Y"],
    ["--do", "X", "--why", "one\ntwo"],
], ids=["no-why", "no-do", "blank-why", "blank-do", "two-line-do", "two-line-why"])
def test_a_missing_or_multi_line_value_changes_nothing(planned, arguments):
    root, cfg, (first, second, third) = planned
    before = _documents(root, cfg)

    result = runner.invoke(app, ["task", "defer", second, *arguments])

    assert result.exit_code != 0
    assert _documents(root, cfg) == before


def test_the_label_is_not_offered_to_task_note(planned):
    root, cfg, (first, second, third) = planned

    result = runner.invoke(app, ["task", "note", first, "--text", "x", "--label", "Deferred"])

    assert result.exit_code != 0
    assert "task defer" in result.output


def test_the_deferred_plan_still_validates(planned):
    root, cfg, (first, second, third) = planned

    runner.invoke(app, ["task", "defer", second, "--do", "X", "--why", "Y"])

    assert plan.validate_plan(root, cfg, "thing") == []
    assert plan.plan_warnings(root, cfg, "thing") == []
