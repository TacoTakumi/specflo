"""Task worktrees when the specflo root is below the git toplevel.

The task records, and its brief names, the root's place inside the
worktree. Included files land at the same place there, so the checkout's
ignore rules cover them and land does not commit them. Land compares the
changed files with Files from the root, and land and reopen remove the
whole worktree.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import config, plan, projects, spec
from specflo.cli import app
from test_worktree_start import SLUG, commit, git

runner = CliRunner()


@pytest.fixture
def nested(tmp_path, monkeypatch):
    """A repo at ``tmp_path`` whose specflo root is ``tmp_path/pkg``."""
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "pkg/src/a.py", "a = 1\n")
    commit(tmp_path, "pkg/.gitignore", ".env\n")
    root = tmp_path / "pkg"
    cfg = config.init_config(root)
    projects.create_project(root, cfg, "Thing")
    spec.start_spec(root, cfg, SLUG)
    spec.add_requirement(root, cfg, SLUG, "Prints help", acceptance="exits 0")
    plan.start_plan(root, cfg, SLUG)
    task = plan.add_task(root, cfg, SLUG, "First", "it works", "uv run pytest",
                         implements=["REQ-01"], files="src/a.py")
    projects.switch_project(root, cfg, SLUG)
    (root / ".env").write_text("TOKEN=abc\n")
    (root / ".specflo" / "worktree-include").write_text(".env\n")
    monkeypatch.chdir(root)
    result = runner.invoke(app, ["task", "start", task.id, "--worktree", "--json"])
    assert result.exit_code == 0, result.output
    top = root / ".specflo" / "worktrees" / SLUG / task.id
    return tmp_path, root, cfg, task.id, top, json.loads(result.output)


def test_the_task_records_the_roots_place_in_the_worktree(nested):
    repo, root, cfg, task_id, top, started = nested
    where = f".specflo/worktrees/{SLUG}/{task_id}/pkg"

    assert started["worktree"] == where
    assert plan.list_tasks(root, cfg, SLUG)[0].worktree == where
    assert f"Worktree:   {root / where}" in runner.invoke(app, ["task", "show", task_id]).output


def test_an_included_file_lands_at_the_roots_place(nested):
    repo, root, cfg, task_id, top, started = nested

    assert (top / "pkg" / ".env").read_text() == "TOKEN=abc\n"
    assert not (top / ".env").exists()


def test_land_keeps_the_included_file_out_and_notes_nothing(nested):
    repo, root, cfg, task_id, top, started = nested
    (top / "pkg" / "src" / "a.py").write_text("a = 2\n")

    result = runner.invoke(app, ["task", "land", task_id, "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["files"] == ["src/a.py"] and data["outside_files"] == []
    assert git(repo, "diff", "--name-only", "HEAD^", "HEAD") == "pkg/src/a.py"
    assert plan.list_tasks(root, cfg, SLUG)[0].notes == []
    assert not top.exists()
    assert f"specflo/{SLUG}/{task_id}" not in git(repo, "branch", "--list")


def test_a_file_outside_the_root_is_noted_from_the_root(nested):
    repo, root, cfg, task_id, top, started = nested
    (top / "pkg" / "src" / "a.py").write_text("a = 2\n")
    (top / "top.txt").write_text("outside\n")

    result = runner.invoke(app, ["task", "land", task_id, "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["outside_files"] == ["../top.txt"]


def test_reopen_removes_the_whole_worktree(nested):
    repo, root, cfg, task_id, top, started = nested
    (top / "pkg" / "src" / "a.py").write_text("a = 2\n")

    result = runner.invoke(app, ["task", "reopen", task_id])

    assert result.exit_code == 0, result.output
    assert ".patch" in result.output
    assert not top.exists()
    assert f"specflo/{SLUG}/{task_id}" not in git(repo, "branch", "--list")
