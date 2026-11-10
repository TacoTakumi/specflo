"""`followup show` prints the task that was deferred to the follow-up.

The follow-up holds one line of what to do. The acceptance, the verify step,
the files and the notes stay in the plan of the project that deferred the
task, and `followup show` prints them below the follow-up, from any project.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import config, followup, plan, projects, spec
from specflo.cli import app

runner = CliRunner()


@pytest.fixture
def deferred(tmp_path, monkeypatch):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    spec.start_spec(tmp_path, cfg, "thing")
    spec.add_requirement(tmp_path, cfg, "thing", "Prints help", acceptance="exits 0")
    plan.start_plan(tmp_path, cfg, "thing")
    first = plan.add_task(tmp_path, cfg, "thing", "Build the parser", "a", "uv run pytest",
                          implements=["REQ-01"]).id
    second = plan.add_task(
        tmp_path, cfg, "thing", "Build the printer", "the page prints in colour",
        "uv run pytest tests/test_printer.py", implements=["REQ-01"],
        files="src/printer.py, tests/test_printer.py", scope="Small",
    ).id
    plan.add_note(tmp_path, cfg, "thing", second, "the format has two variants")
    plan.add_note(tmp_path, cfg, "thing", second, "variant B needs a licence", label="Design")
    projects.switch_project(tmp_path, cfg, "thing")
    monkeypatch.chdir(tmp_path)
    plain = followup.add_followup(tmp_path, cfg, "thing", "Flaky test", "Fix it")
    _, entry = followup.defer_task(tmp_path, cfg, "thing", second, "Build it later", "No time")
    return tmp_path, cfg, second, entry.id, plain.id


def test_show_prints_the_task_below_the_followup(deferred):
    root, cfg, task_id, followup_id, _ = deferred

    result = runner.invoke(app, ["followup", "show", followup_id])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "    Status: open" in out
    assert out.index("Status: open") < out.index(f"{task_id} - Build the printer")
    assert "the page prints in colour" in out
    assert "uv run pytest tests/test_printer.py" in out
    assert "REQ-01" in out
    assert "src/printer.py, tests/test_printer.py" in out
    notes = ["the format has two variants", "[Design] variant B needs a licence",
             "[Deferred] No time"]
    positions = [out.index(note) for note in notes]
    assert positions == sorted(positions)


def test_json_carries_the_task(deferred):
    root, cfg, task_id, followup_id, _ = deferred

    payload = json.loads(runner.invoke(app, ["followup", "show", followup_id, "--json"]).output)

    task = payload["task"]
    assert task["id"] == task_id
    assert task["text"] == "Build the printer"
    assert task["acceptance"] == "the page prints in colour"
    assert task["verify"] == "uv run pytest tests/test_printer.py"
    assert task["implements"] == ["REQ-01"]
    assert task["files"] == ["src/printer.py", "tests/test_printer.py"]
    assert [n["text"] for n in task["notes"]] == [
        "the format has two variants", "variant B needs a licence", "No time",
    ]
    assert payload["from"] == f"{task_id} (deferred)"


def test_a_followup_no_task_was_deferred_to_prints_as_before(deferred):
    root, cfg, _, _, plain_id = deferred

    text = runner.invoke(app, ["followup", "show", plain_id]).output
    payload = json.loads(runner.invoke(app, ["followup", "show", plain_id, "--json"]).output)

    assert text == f"{plain_id}  thing  Flaky test\n    Do: Fix it\n    Status: open\n"
    assert payload["task"] is None


def test_the_task_is_shown_when_another_project_is_active(deferred):
    root, cfg, task_id, followup_id, _ = deferred
    here = runner.invoke(app, ["followup", "show", followup_id]).output
    projects.create_project(root, cfg, "Other")
    projects.switch_project(root, cfg, "other")

    there = runner.invoke(app, ["followup", "show", followup_id])

    assert there.exit_code == 0, there.output
    assert there.output == here
    assert "the page prints in colour" in there.output


def test_a_closed_followup_still_shows_the_task(deferred):
    root, cfg, task_id, followup_id, _ = deferred
    followup.close_followup(root, cfg, followup_id, "Built in the printer project")

    result = runner.invoke(app, ["followup", "show", followup_id])

    assert "Status: closed" in result.output
    assert "the page prints in colour" in result.output


def test_a_task_that_only_mentions_the_followup_is_not_shown(deferred):
    root, cfg, task_id, followup_id, plain_id = deferred
    first = plan.list_tasks(root, cfg, "thing")[0].id
    plan.add_note(root, cfg, "thing", first, f"see {plain_id} for the flaky test")

    payload = json.loads(runner.invoke(app, ["followup", "show", plain_id, "--json"]).output)

    assert payload["task"] is None
