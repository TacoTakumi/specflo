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


# --- climbing: a completed level moves the ladder up ---------------------------------


def _work(repo, name, text="work\n"):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"work on {name}")


def _finish_quick(repo):
    _ok(["section", "set", "brief", "Goal", "--stdin"], "Fix the greeting.\n")
    _ok(["section", "set", "brief", "Done when", "--stdin"], "- app.txt says hi\n")
    _work(repo, "app.txt", "hi\n")
    _ok(["section", "set", "brief", "Proof", "--stdin"], "cat app.txt -> hi\n")
    _ok(["advance"])


def _ladder_at_quick(repo):
    _ok(["new", "Thing", "--level", "quick"])
    _ok(["auto", "--ladder", "--json"])


def test_a_completed_quick_level_moves_the_ladder_to_fast(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    quick_end = git(repo, "rev-parse", "HEAD")

    result = json.loads(_ok(["auto", "--json"]).output)

    assert result["stop"] is False
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "specflo/thing/fast"
    assert git(repo, "rev-parse", "specflo/thing/quick") == quick_end
    project = projects.load_project(repo, config.load_config(repo), "thing")
    assert (project.level, project.phase, project.status) == ("fast", "brainstorm", "active")
    ladder_md = (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()
    assert "| quick |" in ladder_md


def _finish_fast(repo):
    """Take a seeded fast project through its phases, work and a review."""
    _ok(["decision", "add", "--text", "keep it small", "--rationale", "weighed a, b"])
    _ok(["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], "none\n")
    _ok(["advance"])
    _ok(["section", "set", "spec", "In scope", "--stdin"], "- the greeting.\n")
    _ok(["section", "set", "spec", "Out of scope", "--stdin"], "- the rest.\n")
    _ok(["advance"])
    _ok(["advance"])
    _work(repo, "fast.txt")
    _ok(["review", "start"])
    _ok(["review", "done", "--verdict", "ready-to-merge"])
    _ok(["advance"])


def test_a_completed_fast_level_moves_the_ladder_to_full(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    _ok(["auto", "--json"])
    _finish_fast(repo)
    fast_end = git(repo, "rev-parse", "HEAD")

    result = json.loads(_ok(["auto", "--json"]).output)

    assert result["stop"] is False
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "specflo/thing/full"
    assert git(repo, "rev-parse", "specflo/thing/fast") == fast_end
    project = projects.load_project(repo, config.load_config(repo), "thing")
    assert (project.level, project.phase) == ("full", "brainstorm")
    assert "| fast |" in (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()


# --- caps inside a ladder: cut down, do not stop ---------------------------------------


def test_a_quick_ladder_level_over_its_cap_is_told_to_cut_down(repo):
    _ladder_at_quick(repo)
    _ok(["section", "set", "brief", "Done when", "--stdin"], "- one\n- two\n")

    result = json.loads(_ok(["auto", "--json"]).output)

    assert result["stop"] is False
    assert "one check" in result["payload"] and "Deferred" in result["payload"]


def test_a_fast_ladder_level_over_its_cap_is_told_to_cut_down(repo):
    _ladder_at_quick(repo)
    _finish_quick(repo)
    _ok(["auto", "--json"])
    for n in range(7):
        _ok(["task", "add", "--text", f"more {n}", "--acceptance", "a", "--verify", "true",
             "--from", "REQ-01"])

    result = json.loads(_ok(["auto", "--json"]).output)

    assert result["stop"] is False
    assert "7" in result["payload"] and "Out of scope / Deferred" in result["payload"]


# --- the ladder.md rows ---------------------------------------------------------------


def _row(repo, level):
    text = (repo / "docs" / "projects" / "thing" / "ladder.md").read_text()
    line = next(l for l in text.splitlines() if l.startswith(f"| {level} |"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    keys = ["level", "branch", "commits", "files", "added", "removed", "tasks",
            "tests", "review", "deferred", "time"]
    return dict(zip(keys, cells))


def _quick_level_with_history(repo):
    _ladder_at_quick(repo)
    base = _state(repo)["ladder"]["base_commit"]
    _ok(["section", "set", "brief", "Goal", "--stdin"], "Grow the file.\n")
    _ok(["section", "set", "brief", "Done when", "--stdin"], "- the file has ten lines\n")
    _ok(["section", "set", "brief", "Deferred", "--stdin"], "- one more\n- and another\n")
    _work(repo, "app.txt", "".join(f"line {n}\n" for n in range(5)))
    _work(repo, "app.txt", "".join(f"line {n}\n" for n in range(10)))
    _ok(["section", "set", "brief", "Proof", "--stdin"], "wc -l app.txt -> 10\n")
    _ok(["advance"])
    _ok(["auto", "--json"])
    return base


def test_the_quick_row_matches_git_and_the_brief(repo):
    base = _quick_level_with_history(repo)
    row = _row(repo, "quick")

    assert row["commits"] == git(repo, "rev-list", "--count", f"{base}..specflo/thing/quick") == "2"
    numstat = git(repo, "diff", "--numstat", base, "specflo/thing/quick").splitlines()
    assert row["files"] == str(len(numstat)) == "1"
    plus, minus, _ = numstat[0].split("\t")
    assert (row["added"], row["removed"]) == (plus, minus) == ("10", "1")
    assert (row["tasks"], row["review"], row["tests"]) == ("n/a", "none", "not run")
    assert row["deferred"] == "2"
    assert row["time"].isdigit()


@pytest.mark.parametrize("command, expected", [("true", "pass"), ("false", "fail")])
def test_the_row_records_the_test_command_result(repo, command, expected):
    _ok(["config", "set", "test_command", command])
    _quick_level_with_history(repo)

    assert _row(repo, "quick")["tests"] == expected
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "specflo/thing/fast"
