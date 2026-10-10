"""`task start --worktree` gives a task its own git worktree and branch.

The worktree is at ``.specflo/worktrees/<slug>/<task>`` on a new branch
``specflo/<slug>/<task>`` cut from the checkout's HEAD. The task records the
worktree and its base branch and sha, and the output names the path. A task
in progress already is refused and nothing changes.
"""

import json
import os
import subprocess

import pytest
from typer.testing import CliRunner

from specflo import config, plan, projects, spec
from specflo.cli import app

runner = CliRunner()

SLUG = "thing"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(path, *args):
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args], cwd=path, capture_output=True,
        text=True, check=True, env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


def commit(path, name, text, message=None):
    (path / name).parent.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-qm", message or name)
    return git(path, "rev-parse", "HEAD")


def make_repo(root, tasks=(("First", "src/a.py"),), execution="linear"):
    """A git checkout at ``root`` on branch main holding src/a.py, with an
    active project whose plan has ``tasks`` as ``(title, files)``; their ids."""
    git(root, "init", "-q", "-b", "main")
    commit(root, "src/a.py", "a = 1\n")
    cfg = config.init_config(root)
    projects.create_project(root, cfg, "Thing", execution=execution)
    spec.start_spec(root, cfg, SLUG)
    spec.add_requirement(root, cfg, SLUG, "Prints help", acceptance="exits 0")
    plan.start_plan(root, cfg, SLUG)
    ids = [
        plan.add_task(root, cfg, SLUG, title, "it works", "uv run pytest",
                      implements=["REQ-01"], files=files).id
        for title, files in tasks
    ]
    projects.switch_project(root, cfg, SLUG)
    return cfg, ids


def task(root, cfg, task_id):
    return next(t for t in plan.list_tasks(root, cfg, SLUG) if t.id == task_id)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path, cfg, ids[0]


def test_start_makes_the_worktree_on_its_branch(repo):
    root, cfg, task_id = repo
    head = git(root, "rev-parse", "HEAD")

    result = runner.invoke(app, ["task", "start", task_id, "--worktree"])

    assert result.exit_code == 0, result.output
    path = root / ".specflo" / "worktrees" / SLUG / task_id
    assert path.is_dir() and (path / "src" / "a.py").read_text() == "a = 1\n"
    assert f".specflo/worktrees/{SLUG}/{task_id}" in result.output
    listing = git(root, "worktree", "list", "--porcelain")
    assert f"worktree {path}\nHEAD {head}\nbranch refs/heads/specflo/{SLUG}/{task_id}" in listing


def test_the_task_records_its_worktree_and_base(repo):
    root, cfg, task_id = repo
    head = git(root, "rev-parse", "HEAD")

    assert runner.invoke(app, ["task", "start", task_id, "--worktree"]).exit_code == 0

    started = task(root, cfg, task_id)
    assert started.progress == "in_progress"
    assert started.worktree == f".specflo/worktrees/{SLUG}/{task_id}"
    assert started.base == f"main {head}"
    assert runner.invoke(app, ["validate", "plan"]).exit_code == 0


def test_the_worktrees_stay_out_of_git_status(repo):
    root, cfg, task_id = repo

    assert runner.invoke(app, ["task", "start", task_id, "--worktree"]).exit_code == 0

    assert ".specflo/worktrees" not in git(root, "status", "--porcelain")


def test_json_names_the_worktree(repo):
    root, cfg, task_id = repo

    result = runner.invoke(app, ["task", "start", task_id, "--worktree", "--json"])

    data = json.loads(result.output)
    assert data["worktree"] == f".specflo/worktrees/{SLUG}/{task_id}"
    assert data["branch"] == f"specflo/{SLUG}/{task_id}" and data["base_branch"] == "main"


def test_a_task_in_progress_is_refused(repo):
    root, cfg, task_id = repo
    assert runner.invoke(app, ["task", "start", task_id]).exit_code == 0
    path = plan.plan_path(root, cfg, SLUG)
    before = path.read_bytes()

    result = runner.invoke(app, ["task", "start", task_id, "--worktree"])

    assert result.exit_code != 0 and "in progress already" in result.output
    assert path.read_bytes() == before
    assert not (root / ".specflo" / "worktrees" / SLUG / task_id).exists()


def test_a_refused_start_leaves_no_worktree(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path, tasks=(("First", "src/a.py"), ("Second", "src/b.py")))
    plan.edit_task(tmp_path, cfg, SLUG, ids[1], add_depends_on=[ids[0]])
    monkeypatch.chdir(tmp_path)
    before = plan.plan_path(tmp_path, cfg, SLUG).read_bytes()

    result = runner.invoke(app, ["task", "start", ids[1], "--worktree"])

    assert result.exit_code != 0 and ids[0] in result.output
    assert plan.plan_path(tmp_path, cfg, SLUG).read_bytes() == before
    assert not (tmp_path / ".specflo" / "worktrees" / SLUG / ids[1]).exists()
    assert f"specflo/{SLUG}/{ids[1]}" not in git(tmp_path, "branch", "--list")
