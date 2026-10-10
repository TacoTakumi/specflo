"""`task land` refuses an unsafe state and changes nothing.

It refuses a change under the projects directory, a checkout on another
branch, an uncommitted edit to a file the task changes, a pending task and
a shared-tree task. Each refusal leaves the base branch, the worktree and
its branch as they were. Uncommitted changes to other files, such as
plan.md, do not block it.
"""

import pytest
from typer.testing import CliRunner

from specflo import plan
from specflo.cli import app
from test_worktree_start import SLUG, git, make_repo, task

runner = CliRunner()


@pytest.fixture
def started(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path, tasks=(("First", "src/a.py"), ("Second", "src/b.py")))
    # The project documents are tracked, as they are in a real checkout.
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "project documents")
    monkeypatch.chdir(tmp_path)
    task_id = ids[0]
    assert runner.invoke(app, ["task", "start", task_id, "--worktree"]).exit_code == 0
    path = tmp_path / ".specflo" / "worktrees" / SLUG / task_id
    (path / "src" / "a.py").write_text("a = 2\n")
    return tmp_path, cfg, ids, path


def _state(root, path, task_id):
    return (
        git(root, "rev-parse", "refs/heads/main"),
        git(root, "rev-parse", f"refs/heads/specflo/{SLUG}/{task_id}"),
        git(path, "status", "--porcelain"),
        (path / "src" / "a.py").read_text(),
    )


def _refused(root, cfg, task_id, path, *expected):
    before = _state(root, path, task_id)
    result = runner.invoke(app, ["task", "land", task_id])
    assert result.exit_code != 0, result.output
    for text in expected:
        assert text in result.output, result.output
    assert _state(root, path, task_id) == before
    assert task(root, cfg, task_id).commit is None
    return result


def test_a_change_under_the_projects_dir_is_refused(started):
    root, cfg, ids, path = started
    notes = path / "docs" / "projects" / SLUG / "notes.md"
    notes.write_text("hand written\n")

    _refused(root, cfg, ids[0], path, f"docs/projects/{SLUG}/notes.md")
    assert notes.read_text() == "hand written\n"


def test_a_checkout_on_another_branch_is_refused(started):
    root, cfg, ids, path = started
    git(root, "checkout", "-q", "-b", "other")

    _refused(root, cfg, ids[0], path, "main", "other")


def test_an_uncommitted_edit_to_a_task_file_is_refused(started):
    root, cfg, ids, path = started
    (root / "src" / "a.py").write_text("mine\n")

    _refused(root, cfg, ids[0], path, "src/a.py")
    assert (root / "src" / "a.py").read_text() == "mine\n"


def test_a_pending_task_is_refused(started):
    root, cfg, ids, path = started
    before = git(root, "rev-parse", "HEAD")

    result = runner.invoke(app, ["task", "land", ids[1]])

    assert result.exit_code != 0 and "pending" in result.output
    assert git(root, "rev-parse", "HEAD") == before


def test_a_shared_tree_task_is_refused(started):
    root, cfg, ids, path = started
    assert runner.invoke(app, ["task", "start", ids[1]]).exit_code == 0
    before = git(root, "rev-parse", "HEAD")
    document = plan.plan_path(root, cfg, SLUG).read_bytes()

    result = runner.invoke(app, ["task", "land", ids[1]])

    assert result.exit_code != 0 and "shared tree" in result.output
    assert git(root, "rev-parse", "HEAD") == before
    assert plan.plan_path(root, cfg, SLUG).read_bytes() == document


def test_a_dirty_plan_does_not_block_the_land(started):
    root, cfg, ids, path = started
    plan_md = plan.plan_path(root, cfg, SLUG)
    assert plan_md.relative_to(root).as_posix() in git(root, "status", "--porcelain")

    result = runner.invoke(app, ["task", "land", ids[0]])

    assert result.exit_code == 0, result.output
    text = plan_md.read_text()
    assert f"- Worktree: .specflo/worktrees/{SLUG}/{ids[0]}" in text
    assert f"- Commit: {git(root, 'rev-parse', 'HEAD')}" in text
    assert plan_md.relative_to(root).as_posix() in git(root, "status", "--porcelain")


def test_moving_a_document_out_of_the_projects_dir_is_refused(started):
    root, cfg, ids, path = started
    (path / "docs" / "projects" / SLUG / "spec.md").rename(path / "spec-moved.md")

    _refused(root, cfg, ids[0], path, f"docs/projects/{SLUG}/spec.md")
    assert (root / "docs" / "projects" / SLUG / "spec.md").is_file()


def test_an_uncommitted_edit_to_a_renamed_file_is_refused(started):
    root, cfg, ids, path = started
    (path / "src" / "a.py").rename(path / "src" / "c.py")
    (root / "src" / "a.py").write_text("mine\n")
    before = git(root, "rev-parse", "HEAD")

    result = runner.invoke(app, ["task", "land", ids[0]])

    assert result.exit_code != 0 and "src/a.py" in result.output
    assert git(root, "rev-parse", "HEAD") == before
    assert (root / "src" / "a.py").read_text() == "mine\n"
    assert (path / "src" / "c.py").is_file()
