"""The CLI says when to run task start and task done.

A model that reads a task's brief and starts to code skips the task's state.
The guide's task start and task done lines say when each runs and that the
main agent runs it, and `specflo task show` ends with the next lifecycle step
for the task it shows. A worktree task's brief is a subagent's, and a
subagent never runs specflo, so it gets no such line.
"""

from typer.testing import CliRunner

from specflo import guide
from specflo.cli import app
from test_worktree_start import make_repo

runner = CliRunner()


def _summary(name):
    return next(c["summary"] for c in guide.COMMANDS if c["name"] == name)


def _last_line(task_id):
    result = runner.invoke(app, ["task", "show", task_id])
    assert result.exit_code == 0, result.output
    return result.output.rstrip().splitlines()[-1]


def test_the_guide_says_task_start_runs_before_the_work_by_the_main_agent():
    summary = _summary("task start")
    assert summary.startswith("Run before any work on a task")
    assert "The main agent runs it, never a subagent." in summary


def test_the_guide_says_task_done_runs_after_verify_and_commit_by_the_main_agent():
    summary = _summary("task done")
    assert summary.startswith("Run after the task's Verify passes and its commit lands")
    assert "The main agent runs it." in summary


def test_task_show_for_a_pending_task_ends_with_task_start(tmp_path, monkeypatch):
    _, ids = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert _last_line(ids[0]) == f"Next: `specflo task start {ids[0]}` before you change a file."


def test_task_show_for_an_in_progress_task_ends_with_verify_commit_done(tmp_path, monkeypatch):
    _, ids = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0]]).exit_code == 0

    assert _last_line(ids[0]) == (
        f"Next: run the Verify, commit the task's files, then `specflo task done {ids[0]}`."
    )


def test_task_show_for_a_worktree_task_names_no_lifecycle_step(tmp_path, monkeypatch):
    _, ids = make_repo(tmp_path, execution="fan-out")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0], "--worktree"]).exit_code == 0

    output = runner.invoke(app, ["task", "show", ids[0]]).output
    assert "Next:" not in output
    assert "specflo task done" not in output


def test_task_show_for_a_done_task_names_no_lifecycle_step(tmp_path, monkeypatch):
    _, ids = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0]]).exit_code == 0
    assert runner.invoke(app, ["task", "done", ids[0]]).exit_code == 0

    output = runner.invoke(app, ["task", "show", ids[0]]).output
    assert "Next: `specflo task start" not in output
    assert "Next: run the Verify" not in output
