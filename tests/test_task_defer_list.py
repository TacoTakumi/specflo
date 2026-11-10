"""`task list` and `task show` report a deferred task.

The default list is the work of the plan, so a deferred task is left out;
`--all` shows it with the follow-up it went to. `task show` still prints the
entry and says where the work went.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import config, followup, plan, projects, spec
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
        plan.add_task(tmp_path, cfg, "thing", title, "it prints", "uv run pytest",
                      implements=["REQ-01"]).id
        for title in ("Build the parser", "Build the printer", "Build the linter")
    ]
    projects.switch_project(tmp_path, cfg, "thing")
    monkeypatch.chdir(tmp_path)
    _, entry = followup.defer_task(tmp_path, cfg, "thing", ids[1], "Build it later", "No time")
    return tmp_path, cfg, ids, entry.id


def _rows(output, task_id):
    # A row is '<marker> T-NN  [...]'; the deferred line names the id too.
    return [line for line in output.splitlines() if line[1:].startswith(f" {task_id} ")]


def test_the_default_list_names_the_deferred_tasks(planned):
    root, cfg, (first, second, third), followup_id = planned
    _, other = followup.defer_task(root, cfg, "thing", first, "Parse it later", "No time")

    result = runner.invoke(app, ["task", "list"])

    assert result.exit_code == 0, result.output
    assert (
        f"2 deferred: {first} ({other.id}), {second} ({followup_id}). "
        "See them with `specflo task list --all`."
    ) in result.output


def test_no_deferred_line_with_all_or_with_no_deferral(planned):
    root, cfg, (first, second, third), followup_id = planned
    assert "deferred:" not in runner.invoke(app, ["task", "list", "--all"]).output

    projects.create_project(root, cfg, "Other")
    spec.start_spec(root, cfg, "other")
    spec.add_requirement(root, cfg, "other", "Prints help", acceptance="exits 0")
    plan.start_plan(root, cfg, "other")
    plan.add_task(root, cfg, "other", "Build it", "a", "uv run pytest", implements=["REQ-01"])
    projects.switch_project(root, cfg, "other")
    result = runner.invoke(app, ["task", "list"])
    assert result.exit_code == 0, result.output
    assert "deferred" not in result.output


def test_the_default_list_leaves_the_deferred_task_out(planned):
    root, cfg, (first, second, third), followup_id = planned

    result = runner.invoke(app, ["task", "list"])

    assert result.exit_code == 0, result.output
    assert _rows(result.output, second) == []
    assert _rows(result.output, first) and _rows(result.output, third)
    assert "0/2 done" in result.output


def test_all_marks_the_row_with_the_followup(planned):
    root, cfg, (first, second, third), followup_id = planned

    result = runner.invoke(app, ["task", "list", "--all"])

    [row] = _rows(result.output, second)
    assert f"(deferred to {followup_id})" in row
    assert "(superseded)" not in row
    assert "deferred" not in _rows(result.output, first)[0]


def test_a_superseded_row_keeps_its_mark(planned):
    root, cfg, (first, second, third), followup_id = planned
    plan.add_task(root, cfg, "thing", "Build the linter again", "a", "uv run pytest",
                  implements=["REQ-01"], supersedes=third)

    result = runner.invoke(app, ["task", "list", "--all"])

    [row] = _rows(result.output, third)
    assert "(superseded)" in row and "deferred" not in row


def test_json_gives_the_status_and_the_followup(planned):
    root, cfg, (first, second, third), followup_id = planned

    result = runner.invoke(app, ["task", "list", "--all", "--json"])

    tasks = {t["id"]: t for t in json.loads(result.output)["tasks"]}
    assert tasks[second]["status"] == "deferred"
    assert tasks[second]["deferred_to"] == followup_id
    assert tasks[second]["ready"] is False and tasks[second]["next"] is False
    assert tasks[first]["status"] == "active"
    assert "deferred_to" not in tasks[first]
    default = json.loads(runner.invoke(app, ["task", "list", "--json"]).output)
    assert second not in [t["id"] for t in default["tasks"]]


def test_show_prints_the_entry_and_where_the_work_went(planned):
    root, cfg, (first, second, third), followup_id = planned

    result = runner.invoke(app, ["task", "show", second])

    assert result.exit_code == 0, result.output
    assert f"deferred to {followup_id}" in result.output
    assert f"specflo followup show {followup_id}" in result.output
    assert "it prints" in result.output
    assert "No time" in result.output


def test_show_json_names_the_followup(planned):
    root, cfg, (first, second, third), followup_id = planned

    deferred = json.loads(runner.invoke(app, ["task", "show", second, "--json"]).output)
    active = json.loads(runner.invoke(app, ["task", "show", first, "--json"]).output)

    assert deferred["task"]["deferred_to"] == followup_id
    assert "deferred_to" not in active["task"]


def test_show_of_an_active_task_does_not_mention_deferral(planned):
    root, cfg, (first, second, third), followup_id = planned

    result = runner.invoke(app, ["task", "show", first])

    assert result.exit_code == 0, result.output
    assert "eferred" not in result.output


def test_show_with_no_argument_never_picks_the_deferred_task(planned):
    root, cfg, (first, second, third), followup_id = planned

    result = runner.invoke(app, ["task", "show"])

    assert result.output.startswith(f"{first} - ")
