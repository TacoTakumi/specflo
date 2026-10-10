"""`task land` stops cleanly on a conflict with the base branch.

When the base branch and the worktree change the same line, land names the
file and exits non-zero. The base branch is unchanged, the worktree has no
rebase in progress and still holds the change, and the task has no Commit.
"""

import subprocess

from typer.testing import CliRunner

from specflo.cli import app
from test_worktree_start import SLUG, commit, git, make_repo, task

runner = CliRunner()


def test_a_conflict_names_the_file_and_changes_nothing(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    task_id = ids[0]
    assert runner.invoke(app, ["task", "start", task_id, "--worktree"]).exit_code == 0
    path = tmp_path / ".specflo" / "worktrees" / SLUG / task_id
    (path / "src" / "a.py").write_text("a = 'worktree'\n")
    tip = commit(tmp_path, "src/a.py", "a = 'main'\n")

    result = runner.invoke(app, ["task", "land", task_id])

    assert result.exit_code != 0
    assert "src/a.py" in result.output and "conflicts" in result.output
    assert git(tmp_path, "rev-parse", "HEAD") == tip
    assert (tmp_path / "src" / "a.py").read_text() == "a = 'main'\n"
    assert git(tmp_path, "status", "--porcelain", "--untracked-files=no") == ""
    assert path.is_dir()
    assert (path / "src" / "a.py").read_text() == "a = 'worktree'\n"
    git_dir = git(path, "rev-parse", "--git-dir")
    assert not (path / git_dir / "rebase-merge").exists()
    assert not (path / git_dir / "rebase-apply").exists()
    assert f"specflo/{SLUG}/{task_id}" in git(tmp_path, "branch", "--list")
    assert task(tmp_path, cfg, task_id).commit is None


def test_a_conflict_on_a_non_ascii_name_is_named_unquoted(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path)
    name = "src/caf\u00e9.py"
    commit(tmp_path, name, "x = 0\n")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0], "--worktree"]).exit_code == 0
    path = tmp_path / ".specflo" / "worktrees" / SLUG / ids[0]
    (path / name).write_text("x = 'worktree'\n")
    commit(tmp_path, name, "x = 'main'\n")

    result = runner.invoke(app, ["task", "land", ids[0]])

    assert result.exit_code != 0
    assert name in result.output and "\\303" not in result.output


def _conflicted(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "project documents")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0], "--worktree"]).exit_code == 0
    path = tmp_path / ".specflo" / "worktrees" / SLUG / ids[0]
    (path / "src" / "a.py").write_text("a = 'worktree'\n")
    tip = commit(tmp_path, "src/a.py", "a = 'main'\n")
    assert runner.invoke(app, ["task", "land", ids[0]]).exit_code != 0
    return cfg, ids[0], path, tip


def test_merging_the_base_into_the_worktree_resolves_the_conflict(tmp_path, monkeypatch):
    cfg, task_id, path, tip = _conflicted(tmp_path, monkeypatch)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "the task's change")
    subprocess.run(["git", "merge", "-q", "main"], cwd=path, capture_output=True)
    (path / "src" / "a.py").write_text("a = 'both'\n")
    git(path, "add", "src/a.py")
    git(path, "commit", "-qm", "resolve")

    result = runner.invoke(app, ["task", "land", task_id])

    assert result.exit_code == 0, result.output
    assert git(tmp_path, "rev-parse", "HEAD^") == tip
    assert (tmp_path / "src" / "a.py").read_text() == "a = 'both'\n"


def test_resetting_the_worktree_to_the_tip_resolves_the_conflict(tmp_path, monkeypatch):
    cfg, task_id, path, tip = _conflicted(tmp_path, monkeypatch)
    git(path, "reset", "-q", "--hard", "main")
    (path / "src" / "a.py").write_text("a = 'both'\n")

    result = runner.invoke(app, ["task", "land", task_id])

    assert result.exit_code == 0, result.output
    assert git(tmp_path, "rev-parse", "HEAD^") == tip
    assert (tmp_path / "src" / "a.py").read_text() == "a = 'both'\n"


def test_a_merged_document_commit_does_not_trip_the_guard(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "project documents")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0], "--worktree"]).exit_code == 0
    path = tmp_path / ".specflo" / "worktrees" / SLUG / ids[0]
    (path / "src" / "a.py").write_text("a = 2\n")
    tip = commit(tmp_path, f"docs/projects/{SLUG}/notes.md", "kept by specflo\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "the task's change")
    git(path, "merge", "-q", "--no-edit", "main")

    result = runner.invoke(app, ["task", "land", ids[0]])

    assert result.exit_code == 0, result.output
    assert git(tmp_path, "rev-parse", "HEAD^") == tip
    assert git(tmp_path, "diff", "--name-only", "HEAD^", "HEAD") == "src/a.py"
