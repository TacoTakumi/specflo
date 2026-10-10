"""`.specflo/worktree-include` lists files git does not carry into a new worktree.

Each listed path that exists is copied with the same bytes; one that does
not exist is named in the output. With no include file nothing is copied
and nothing is reported.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo.cli import app
from test_worktree_start import SLUG, make_repo

runner = CliRunner()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    cfg, ids = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path, ids[0]


def test_listed_files_are_copied_and_a_missing_one_is_named(repo):
    root, task_id = repo
    (root / ".env").write_bytes(b"TOKEN=abc\n\x00raw")
    (root / "conf").mkdir()
    (root / "conf" / "local.toml").write_text("x = 1\n")
    (root / ".specflo" / "worktree-include").write_text(
        "# local files\n.env\n\nconf\nmissing.txt\n../outside\n")

    result = runner.invoke(app, ["task", "start", task_id, "--worktree"])

    assert result.exit_code == 0, result.output
    path = root / ".specflo" / "worktrees" / SLUG / task_id
    assert (path / ".env").read_bytes() == b"TOKEN=abc\n\x00raw"
    assert (path / "conf" / "local.toml").read_text() == "x = 1\n"
    assert "missing.txt" in result.output and "../outside" in result.output
    assert "not copied: .env" not in result.output
    assert "not copied: conf" not in result.output


def test_json_lists_what_was_not_copied(repo):
    root, task_id = repo
    (root / ".specflo" / "worktree-include").write_text("missing.txt\n")

    result = runner.invoke(app, ["task", "start", task_id, "--worktree", "--json"])

    assert json.loads(result.output)["not_copied"] == ["missing.txt"]


def test_no_include_file_copies_and_reports_nothing(repo):
    root, task_id = repo
    (root / ".env").write_text("TOKEN=abc\n")

    result = runner.invoke(app, ["task", "start", task_id, "--worktree"])

    assert result.exit_code == 0, result.output
    assert not (root / ".specflo" / "worktrees" / SLUG / task_id / ".env").exists()
    assert "not copied" not in result.output.lower()
