"""The ladder run: quick, then fast, then full, each on its own stacked branch."""

import json
import subprocess

import pytest
from typer.testing import CliRunner

from specflo import auto, config, projects
from specflo.cli import app

runner = CliRunner()


def _ok(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repository with one commit and specflo initialised, as the cwd."""
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Tester")
    (tmp_path / "app.txt").write_text("hello\n")
    _ok(["init"])
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _branches(repo):
    return set(git(repo, "branch", "--format=%(refname:short)").splitlines())


def _state(repo):
    return auto.load_run_state(repo, config.load_config(repo), "thing")


# --- starting a ladder ---------------------------------------------------------------


def test_ladder_start_cuts_the_quick_branch_and_records_the_base(repo):
    _ok(["new", "Thing", "--level", "quick"])
    base = git(repo, "rev-parse", "HEAD")

    result = runner.invoke(app, ["auto", "--ladder", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["stop"] is False
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "specflo/thing/quick"
    assert git(repo, "rev-parse", "specflo/thing/quick") == base
    ladder_md = (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()
    assert "`main`" in ladder_md and base in ladder_md
    assert _state(repo)["ladder"]["base_commit"] == base


def test_a_later_pass_without_the_flag_continues_the_ladder(repo):
    _ok(["new", "Thing", "--level", "quick"])
    _ok(["auto", "--ladder", "--json"])

    result = json.loads(_ok(["auto", "--json"]).output)

    assert result["stop"] is False
    assert auto.LADDER_MARKER in result["payload"]


def test_ladder_start_refuses_a_fast_project(repo):
    _ok(["new", "Thing", "--level", "fast"])
    before = _branches(repo)

    result = runner.invoke(app, ["auto", "--ladder"])

    assert result.exit_code != 0
    assert "quick" in result.output
    assert _branches(repo) == before


def test_ladder_start_refuses_a_dirty_tree(repo):
    _ok(["new", "Thing", "--level", "quick"])
    (repo / "app.txt").write_text("changed\n")
    before = _branches(repo)

    result = runner.invoke(app, ["auto", "--ladder"])

    assert result.exit_code != 0
    assert "app.txt" in result.output
    assert _branches(repo) == before


def test_ladder_start_refuses_an_existing_ladder_branch(repo):
    _ok(["new", "Thing", "--level", "quick"])
    git(repo, "branch", "specflo/thing/quick")
    before = {b: git(repo, "rev-parse", b) for b in _branches(repo)}

    result = runner.invoke(app, ["auto", "--ladder"])

    assert result.exit_code != 0
    assert "specflo/thing/quick" in result.output
    assert {b: git(repo, "rev-parse", b) for b in _branches(repo)} == before


def test_ladder_start_changes_no_ref_outside_its_own(repo):
    git(repo, "branch", "keep")
    _ok(["new", "Thing", "--level", "quick"])
    before = {b: git(repo, "rev-parse", b) for b in _branches(repo)}

    _ok(["auto", "--ladder", "--json"])

    after = {b: git(repo, "rev-parse", b) for b in _branches(repo)}
    assert {b: after[b] for b in before} == before
    assert set(after) - set(before) == {"specflo/thing/quick"}
