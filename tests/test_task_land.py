"""`task land` turns a worktree task's changes into one commit on its base branch.

What the worktree holds, committed or not, becomes exactly one new commit
whose parent is the base branch's tip, even when the branch moved on since
the task started. The checkout is fast-forwarded to it, the task records it
and stays in progress, and the worktree and its branch are removed.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import guide, plan
from specflo.cli import app
from test_worktree_start import SLUG, commit, git, make_repo, task

runner = CliRunner()


@pytest.fixture
def started(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    task_id = ids[0]
    assert runner.invoke(app, ["task", "start", task_id, "--worktree"]).exit_code == 0
    path = tmp_path / ".specflo" / "worktrees" / SLUG / task_id
    (path / "src" / "a.py").write_text("a = 2\n")
    (path / "src" / "new.py").write_text("new = True\n")
    return tmp_path, cfg, task_id, path


def test_land_adds_one_commit_on_the_moved_branch(started):
    root, cfg, task_id, path = started
    tip = commit(root, "README.txt", "unrelated\n")

    result = runner.invoke(app, ["task", "land", task_id])

    assert result.exit_code == 0, result.output
    head = git(root, "rev-parse", "HEAD")
    assert git(root, "rev-parse", "HEAD^") == tip
    assert sorted(git(root, "diff", "--name-only", tip, head).splitlines()) == [
        "src/a.py", "src/new.py"]
    assert (root / "src" / "a.py").read_text() == "a = 2\n"
    assert (root / "src" / "new.py").read_text() == "new = True\n"
    assert (root / "README.txt").read_text() == "unrelated\n"
    landed = task(root, cfg, task_id)
    assert landed.commit == head and landed.progress == "in_progress"


def test_land_removes_the_worktree_and_branch(started):
    root, cfg, task_id, path = started

    assert runner.invoke(app, ["task", "land", task_id]).exit_code == 0

    assert not path.exists()
    assert f"specflo/{SLUG}/{task_id}" not in git(root, "branch", "--list")
    assert str(path) not in git(root, "worktree", "list")


def test_without_message_the_message_is_the_title(started):
    root, cfg, task_id, path = started

    assert runner.invoke(app, ["task", "land", task_id]).exit_code == 0

    assert git(root, "log", "-1", "--format=%B").strip() == "First"


def test_message_sets_the_commit_message(started):
    root, cfg, task_id, path = started

    result = runner.invoke(app, ["task", "land", task_id, "--message", "Parse the flag"])

    assert result.exit_code == 0, result.output
    assert git(root, "log", "-1", "--format=%B").strip() == "Parse the flag"


def test_a_subagent_commit_is_squashed_into_the_one_commit(started):
    root, cfg, task_id, path = started
    before = git(root, "rev-parse", "HEAD")
    git(path, "add", "src/a.py")
    git(path, "commit", "-qm", "work in progress")

    assert runner.invoke(app, ["task", "land", task_id]).exit_code == 0

    assert git(root, "rev-parse", "HEAD^") == before
    assert git(root, "rev-list", "--count", f"{before}..HEAD") == "1"
    assert sorted(git(root, "diff", "--name-only", before, "HEAD").splitlines()) == [
        "src/a.py", "src/new.py"]


def test_json_carries_the_commit_and_files(started):
    root, cfg, task_id, path = started

    result = runner.invoke(app, ["task", "land", task_id, "--json"])

    data = json.loads(result.output)
    assert data["commit"] == git(root, "rev-parse", "HEAD")
    assert sorted(data["files"]) == ["src/a.py", "src/new.py"] and data["branch"] == "main"


def test_a_worktree_with_no_change_is_refused(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0], "--worktree"]).exit_code == 0
    before = git(tmp_path, "rev-parse", "HEAD")

    result = runner.invoke(app, ["task", "land", ids[0]])

    assert result.exit_code != 0 and "no change" in result.output
    assert git(tmp_path, "rev-parse", "HEAD") == before
    assert task(tmp_path, cfg, ids[0]).commit is None


def test_the_guide_lists_the_verb():
    entries = {entry["name"]: entry for entry in guide.COMMANDS}
    assert entries["task land"]["args"] == "<T-NN> [--message <text>]"
    assert "--worktree" in entries["task start"]["args"]


def test_a_tag_named_like_the_branch_does_not_stop_the_land(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path)
    git(tmp_path, "tag", "main")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0], "--worktree"]).exit_code == 0
    assert task(tmp_path, cfg, ids[0]).base.split()[0] == "main"
    path = tmp_path / ".specflo" / "worktrees" / SLUG / ids[0]
    (path / "src" / "a.py").write_text("a = 2\n")

    result = runner.invoke(app, ["task", "land", ids[0]])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "src" / "a.py").read_text() == "a = 2\n"
