"""A task in its own worktree holds no files in the ready set.

Two tasks that share a file with no edge between them: while one runs in a
worktree the other is ready, and while one runs in the shared tree the
other is not. A landed worktree task, whose fields are kept, holds none.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import plan
from specflo.cli import app
from test_worktree_start import SLUG, make_repo

runner = CliRunner()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path, tasks=(("One", "src/a.py"), ("Two", "src/a.py")))
    monkeypatch.chdir(tmp_path)
    return tmp_path, cfg, ids


def _ready(task_id):
    result = runner.invoke(app, ["task", "list", "--json"])
    assert result.exit_code == 0, result.output
    return next(t for t in json.loads(result.output)["tasks"] if t["id"] == task_id)["ready"]


def test_a_worktree_task_leaves_its_sharer_ready(repo):
    root, cfg, (first, second) = repo

    assert runner.invoke(app, ["task", "start", first, "--worktree"]).exit_code == 0

    assert _ready(second) is True


def test_a_shared_tree_task_holds_its_files(repo):
    root, cfg, (first, second) = repo

    assert runner.invoke(app, ["task", "start", first]).exit_code == 0

    assert _ready(second) is False


def test_a_landed_worktree_task_still_holds_none(repo):
    root, cfg, (first, second) = repo
    # A landed task keeps its Worktree and Base fields and stays in progress.
    plan.start_task(root, cfg, SLUG, first, worktree=f".specflo/worktrees/{SLUG}/{first}",
                    base="main " + "0" * 40)

    assert _ready(second) is True
