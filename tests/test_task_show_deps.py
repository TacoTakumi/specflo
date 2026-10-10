"""`task show` carries what each dependency left behind.

For each task the shown task depends on, the brief prints its id, title,
recorded commit (or that none is recorded) and every note, and the JSON
form has the same data in a dependencies list. A task with no dependencies
prints no dependency block.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import config, plan, projects, spec
from specflo.cli import app

runner = CliRunner()

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def planned(tmp_path, monkeypatch):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    spec.start_spec(tmp_path, cfg, "thing")
    spec.add_requirement(tmp_path, cfg, "thing", "Prints help", acceptance="exits 0")
    plan.start_plan(tmp_path, cfg, "thing")
    first = plan.add_task(tmp_path, cfg, "thing", "Build the parser", "a", "uv run pytest",
                          implements=["REQ-01"])
    second = plan.add_task(tmp_path, cfg, "thing", "Use the parser", "b", "uv run pytest",
                           implements=["REQ-01"], depends_on=[first.id])
    projects.switch_project(tmp_path, cfg, "thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path, cfg, first.id, second.id


def _finish_with_notes(root, cfg, task_id):
    plan.start_task(root, cfg, "thing", task_id)
    plan.add_note(root, cfg, "thing", task_id, "the parser returns a tuple")
    plan.done_task(root, cfg, "thing", task_id, note="kept the old name", commit=SHA)


def test_show_prints_each_dependency_with_its_commit_and_notes(planned):
    root, cfg, first, second = planned
    _finish_with_notes(root, cfg, first)

    result = runner.invoke(app, ["task", "show", second])

    assert result.exit_code == 0, result.output
    assert "Dependencies:" in result.output
    assert f"{first} - Build the parser" in result.output
    assert SHA in result.output
    assert "the parser returns a tuple" in result.output
    assert "kept the old name" in result.output


def test_json_carries_a_dependencies_list(planned):
    root, cfg, first, second = planned
    _finish_with_notes(root, cfg, first)

    result = runner.invoke(app, ["task", "show", second, "--json"])

    assert result.exit_code == 0, result.output
    (dep,) = json.loads(result.output)["dependencies"]
    assert dep["id"] == first and dep["title"] == "Build the parser"
    assert dep["commit"] == SHA
    assert [n["text"] for n in dep["notes"]] == ["the parser returns a tuple",
                                                 "kept the old name"]


def test_a_dependency_with_no_commit_says_so(planned):
    root, cfg, first, second = planned

    result = runner.invoke(app, ["task", "show", second])

    assert result.exit_code == 0, result.output
    assert f"{first} - Build the parser  (no commit recorded)" in result.output


def test_a_task_with_no_dependencies_prints_no_block(planned):
    root, cfg, first, second = planned

    result = runner.invoke(app, ["task", "show", first])

    assert result.exit_code == 0, result.output
    assert "Dependencies:" not in result.output
    assert json.loads(runner.invoke(app, ["task", "show", first, "--json"]).output)[
        "dependencies"] == []
