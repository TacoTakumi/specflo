"""`task done --commit` records the task's commit as a full sha.

The value is resolved in the checkout's git repository, so a short sha or a
ref like HEAD is stored as the full sha it names. A value that names no
commit is refused and plan.md is left as it was.
"""

import subprocess

import pytest
from typer.testing import CliRunner

from specflo import config, plan, projects, spec
from specflo.cli import app

runner = CliRunner()


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def started(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "a.txt").write_text("a\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    spec.start_spec(tmp_path, cfg, "thing")
    spec.add_requirement(tmp_path, cfg, "thing", "Prints help", acceptance="exits 0")
    plan.start_plan(tmp_path, cfg, "thing")
    task = plan.add_task(tmp_path, cfg, "thing", "First", "a", "uv run pytest",
                         implements=["REQ-01"])
    plan.start_task(tmp_path, cfg, "thing", task.id)
    projects.switch_project(tmp_path, cfg, "thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path, cfg, task.id


def test_done_with_commit_records_the_full_sha(started):
    root, cfg, task_id = started
    head = _git(root, "rev-parse", "HEAD")

    result = runner.invoke(app, ["task", "done", task_id, "--commit", "HEAD"])

    assert result.exit_code == 0, result.output
    document = plan.plan_path(root, cfg, "thing").read_text()
    assert f"- Commit: {head}\n" in document
    (task,) = plan.list_tasks(root, cfg, "thing")
    assert task.commit == head and task.progress == "done"
    assert runner.invoke(app, ["validate", "plan"]).exit_code == 0


def test_a_short_sha_is_stored_in_full(started):
    root, cfg, task_id = started
    head = _git(root, "rev-parse", "HEAD")

    result = runner.invoke(app, ["task", "done", task_id, "--commit", head[:8]])

    assert result.exit_code == 0, result.output
    assert plan.list_tasks(root, cfg, "thing")[0].commit == head


def test_a_ref_that_names_no_commit_is_refused(started):
    root, cfg, task_id = started
    path = plan.plan_path(root, cfg, "thing")
    before = path.read_bytes()

    result = runner.invoke(app, ["task", "done", task_id, "--commit", "nosuchref"])

    assert result.exit_code != 0
    assert "nosuchref" in result.output
    assert path.read_bytes() == before


def test_done_without_commit_writes_no_commit_field(started):
    root, cfg, task_id = started

    assert runner.invoke(app, ["task", "done", task_id]).exit_code == 0

    assert "- Commit:" not in plan.plan_path(root, cfg, "thing").read_text()
    assert plan.list_tasks(root, cfg, "thing")[0].commit is None


def test_the_module_refuses_a_value_that_is_not_a_full_sha(started):
    root, cfg, task_id = started

    with pytest.raises(Exception, match="full commit sha"):
        plan.done_task(root, cfg, "thing", task_id, commit="HEAD")
    assert plan.list_tasks(root, cfg, "thing")[0].progress == "in_progress"
