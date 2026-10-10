"""A fan-out task brief carries the rules a subagent works to.

The brief names the read-only git a subagent may run, forbids `git stash`,
every other git write and specflo, states the test policy, and allows a
pinning test to be updated when the task's own change breaks it. A linear
brief carries none of that.
"""

import pytest
from typer.testing import CliRunner

from specflo import config, plan, projects, spec
from specflo.cli import app
from test_worktree_start import SLUG, make_repo

runner = CliRunner()


def _project(tmp_path, monkeypatch, execution):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", execution=execution)
    spec.start_spec(tmp_path, cfg, "thing")
    spec.add_requirement(tmp_path, cfg, "thing", "Prints help", acceptance="exits 0")
    plan.start_plan(tmp_path, cfg, "thing")
    task = plan.add_task(tmp_path, cfg, "thing", "Build", "a", "uv run pytest",
                         implements=["REQ-01"])
    projects.switch_project(tmp_path, cfg, "thing")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["task", "show", task.id])
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def fan_out_brief(tmp_path, monkeypatch):
    return _project(tmp_path, monkeypatch, "fan-out")


def test_the_brief_names_the_read_only_git(fan_out_brief):
    for command in ("`git diff`", "`git log`", "`git show`", "`git status`"):
        assert command in fan_out_brief


def test_the_brief_forbids_stash_git_writes_and_specflo(fan_out_brief):
    assert "Do not run `git stash` or any other git command that writes" in fan_out_brief
    assert "Do not run specflo" in fan_out_brief


def test_the_brief_states_the_test_policy(fan_out_brief):
    assert "run the Verify step" in fan_out_brief
    assert "one parallel run of the test files" in fan_out_brief
    assert "The full suite runs once, before the review round" in fan_out_brief


def test_the_brief_allows_a_pinning_test_update(fan_out_brief):
    assert "A test that pins a fixed list" in fan_out_brief
    assert "may be updated when this task's own change breaks it" in fan_out_brief


def test_a_linear_brief_carries_none_of_it(tmp_path, monkeypatch):
    brief = _project(tmp_path, monkeypatch, "linear")

    assert "Subagent rules" not in brief
    assert "git stash" not in brief
    assert "one parallel run" not in brief
    assert "pins a fixed list" not in brief


def test_a_worktree_task_brief_names_its_worktree(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path, execution="fan-out")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0], "--worktree"]).exit_code == 0

    brief = runner.invoke(app, ["task", "show", ids[0]]).output

    path = tmp_path / ".specflo" / "worktrees" / SLUG / ids[0]
    assert f"Worktree:   {path}" in brief
    assert "Edit and test only in this worktree, and never commit" in brief


def test_a_shared_tree_task_brief_names_no_worktree(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path, execution="fan-out")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0]]).exit_code == 0

    brief = runner.invoke(app, ["task", "show", ids[0]]).output

    assert "Worktree:" not in brief and ".specflo/worktrees" not in brief
