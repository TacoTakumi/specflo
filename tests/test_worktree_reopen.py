"""`task reopen` of a worktree task saves its changes as a patch, then removes it.

The patch holds what the worktree has against the base sha and applies with
`git apply` there. The worktree and its branch are gone, the task is
pending with no worktree recorded, and it can start in a worktree again.
"""

import json
import re
import subprocess

import pytest
from typer.testing import CliRunner

from specflo.cli import app
from test_worktree_start import SLUG, git, make_repo, task

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


def test_reopen_saves_a_patch_that_reproduces_the_change(started, tmp_path_factory):
    root, cfg, task_id, path = started
    base = task(root, cfg, task_id).base.split()[1]

    result = runner.invoke(app, ["task", "reopen", task_id])

    assert result.exit_code == 0, result.output
    match = re.search(r"(\.specflo/\S+\.patch)", result.output)
    assert match, result.output
    patch = root / match.group(1)
    clone = tmp_path_factory.mktemp("clone")
    subprocess.run(["git", "clone", "-q", str(root), str(clone)], check=True)
    git(clone, "checkout", "-q", base)
    git(clone, "apply", str(patch))
    assert (clone / "src" / "a.py").read_text() == "a = 2\n"
    assert (clone / "src" / "new.py").read_text() == "new = True\n"


def test_reopen_removes_the_worktree_and_branch(started):
    root, cfg, task_id, path = started

    assert runner.invoke(app, ["task", "reopen", task_id]).exit_code == 0

    assert not path.exists()
    assert f"specflo/{SLUG}/{task_id}" not in git(root, "branch", "--list")
    reopened = task(root, cfg, task_id)
    assert reopened.progress == "pending"
    assert reopened.worktree is None and reopened.base is None


def test_json_carries_the_patch(started):
    root, cfg, task_id, path = started

    result = runner.invoke(app, ["task", "reopen", task_id, "--json"])

    data = json.loads(result.output)
    assert data["progress"] == "pending" and data["patch"].endswith(".patch")


def test_the_task_starts_in_a_worktree_again(started):
    root, cfg, task_id, path = started
    assert runner.invoke(app, ["task", "reopen", task_id]).exit_code == 0

    result = runner.invoke(app, ["task", "start", task_id, "--worktree"])

    assert result.exit_code == 0, result.output
    assert (path / "src" / "a.py").read_text() == "a = 1\n"


def test_a_worktree_with_no_change_saves_no_patch(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0], "--worktree"]).exit_code == 0

    result = runner.invoke(app, ["task", "reopen", ids[0]])

    assert result.exit_code == 0, result.output
    assert ".patch" not in result.output
    assert not (tmp_path / ".specflo" / "worktrees" / SLUG / ids[0]).exists()


def test_a_shared_tree_reopen_is_unchanged(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0]]).exit_code == 0

    result = runner.invoke(app, ["task", "reopen", ids[0], "--json"])

    assert json.loads(result.output) == {"id": ids[0], "progress": "pending", "blocked": None}


def _reopen_patch(root, task_id):
    result = runner.invoke(app, ["task", "reopen", task_id])
    assert result.exit_code == 0, result.output
    return root / re.search(r"(\.specflo/\S+\.patch)", result.output).group(1)


def _clone_at(root, sha, tmp_path_factory):
    clone = tmp_path_factory.mktemp("clone")
    subprocess.run(["git", "clone", "-q", str(root), str(clone)], check=True)
    git(clone, "checkout", "-q", sha)
    return clone


def test_a_crlf_and_a_non_utf8_edit_survive_the_patch(tmp_path, monkeypatch, tmp_path_factory):
    cfg, ids = make_repo(tmp_path)
    (tmp_path / "win.txt").write_bytes(b"one\r\ntwo\r\n")
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9\n")
    git(tmp_path, "add", "win.txt", "latin.txt")
    git(tmp_path, "commit", "-qm", "text files")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0], "--worktree"]).exit_code == 0
    path = tmp_path / ".specflo" / "worktrees" / SLUG / ids[0]
    (path / "win.txt").write_bytes(b"one\r\nTWO\r\n")
    (path / "latin.txt").write_bytes(b"caf\xe9 cr\xe8me\n")
    base = task(tmp_path, cfg, ids[0]).base.split()[1]

    patch = _reopen_patch(tmp_path, ids[0])

    clone = _clone_at(tmp_path, base, tmp_path_factory)
    git(clone, "apply", str(patch))
    assert (clone / "win.txt").read_bytes() == b"one\r\nTWO\r\n"
    assert (clone / "latin.txt").read_bytes() == b"caf\xe9 cr\xe8me\n"


def test_the_patch_ignores_diff_config(started, tmp_path_factory):
    root, cfg, task_id, path = started
    git(root, "config", "diff.noprefix", "true")
    git(root, "config", "color.diff", "always")
    git(root, "config", "color.ui", "always")
    base = task(root, cfg, task_id).base.split()[1]

    patch = _reopen_patch(root, task_id)

    clone = _clone_at(root, base, tmp_path_factory)
    git(clone, "apply", str(patch))
    assert (clone / "src" / "a.py").read_text() == "a = 2\n"


def test_a_patch_that_does_not_apply_keeps_the_worktree(started, monkeypatch):
    from specflo import worktree

    root, cfg, task_id, path = started
    monkeypatch.setattr(worktree, "_applies", lambda *args: False)

    result = runner.invoke(app, ["task", "reopen", task_id])

    assert result.exit_code != 0 and "does not apply" in result.output
    assert path.is_dir() and (path / "src" / "a.py").read_text() == "a = 2\n"
    assert f"specflo/{SLUG}/{task_id}" in git(root, "branch", "--list")
    assert task(root, cfg, task_id).progress == "in_progress"
