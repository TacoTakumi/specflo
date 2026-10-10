"""`task land` notes the files its commit changed outside the task's Files.

It does not refuse for them: it prints them and records a note on the task
naming them. A land that stays inside Files adds no note.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo.cli import app
from test_worktree_start import SLUG, make_repo, task

runner = CliRunner()


@pytest.fixture
def started(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0], "--worktree"]).exit_code == 0
    path = tmp_path / ".specflo" / "worktrees" / SLUG / ids[0]
    (path / "src" / "a.py").write_text("a = 2\n")
    return tmp_path, cfg, ids[0], path


def test_a_file_outside_files_is_printed_and_noted(started):
    root, cfg, task_id, path = started
    (path / "src" / "b.py").write_text("b = 1\n")

    result = runner.invoke(app, ["task", "land", task_id])

    assert result.exit_code == 0, result.output
    assert "src/b.py" in result.output
    notes = task(root, cfg, task_id).notes
    assert len(notes) == 1 and "src/b.py" in notes[0]["text"]
    assert "src/a.py" not in notes[0]["text"]


def test_json_lists_the_outside_files(started):
    root, cfg, task_id, path = started
    (path / "src" / "b.py").write_text("b = 1\n")

    result = runner.invoke(app, ["task", "land", task_id, "--json"])

    assert json.loads(result.output)["outside_files"] == ["src/b.py"]


def test_a_land_inside_files_adds_no_note(started):
    root, cfg, task_id, path = started

    result = runner.invoke(app, ["task", "land", task_id])

    assert result.exit_code == 0, result.output
    assert "outside" not in result.output
    assert task(root, cfg, task_id).notes == []


def test_a_non_ascii_file_in_files_is_not_noted(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path, tasks=(("Accents", "src/caf\u00e9.py"),))
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["task", "start", ids[0], "--worktree"]).exit_code == 0
    path = tmp_path / ".specflo" / "worktrees" / SLUG / ids[0]
    (path / "src" / "caf\u00e9.py").write_text("x = 1\n")

    result = runner.invoke(app, ["task", "land", ids[0], "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["files"] == ["src/caf\u00e9.py"]
    assert json.loads(result.output)["outside_files"] == []
    assert task(tmp_path, cfg, ids[0]).notes == []
