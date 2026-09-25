"""The `level` verb: a project moves up to more ceremony, never down."""

import pytest
from typer.testing import CliRunner

from specflo import config, projects
from specflo.cli import app

runner = CliRunner()


def _ok(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result


def _project_dir(tmp_path):
    return tmp_path / "docs" / "projects" / "thing"


def _load(tmp_path):
    return projects.load_project(tmp_path, config.load_config(tmp_path), "thing")


def _fast_project_at_execute(tmp_path):
    """A fast project with three decisions, one requirement and T-01 done."""
    _ok(["init"])
    _ok(["new", "Thing", "--level", "fast"])
    for n in range(1, 4):
        _ok(["decision", "add", "--text", f"choice {n}", "--rationale", "because"])
    _ok(["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], "Nothing else.\n")
    _ok(["advance"])
    _ok(["spec", "start"])
    _ok(["requirement", "add", "--text", "it works", "--acceptance", "it runs"])
    _ok(["section", "set", "spec", "In scope", "--stdin"], "- the thing.\n")
    _ok(["section", "set", "spec", "Out of scope", "--stdin"], "- other things.\n")
    _ok(["advance"])
    _ok(["plan", "start"])
    _ok(["task", "add", "--text", "build it", "--acceptance", "it works",
         "--verify", "true", "--from", "REQ-01"])
    _ok(["advance"])
    _ok(["task", "start", "T-01"])
    _ok(["task", "done", "T-01"])


def _documents(tmp_path):
    return {
        name: (_project_dir(tmp_path) / name).read_bytes()
        for name in ("brainstorm.md", "spec.md", "plan.md")
    }


def test_level_full_moves_a_fast_project_back_to_brainstorm_and_keeps_every_document(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _fast_project_at_execute(tmp_path)
    before = _documents(tmp_path)

    result = _ok(["level", "full"])

    project = _load(tmp_path)
    assert (project.level, project.phase, project.status) == ("full", "brainstorm", "active")
    assert _documents(tmp_path) == before
    for decision in ("D-01", "D-02", "D-03"):
        assert decision in result.output
    assert "confirm or supersede" in result.output


def test_level_full_reopens_a_complete_fast_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _fast_project_at_execute(tmp_path)
    projects.complete_project(tmp_path, config.load_config(tmp_path), "thing")

    _ok(["level", "full"])

    project = _load(tmp_path)
    assert (project.level, project.phase, project.status) == ("full", "brainstorm", "active")


def test_a_superseded_decision_is_not_listed_for_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _fast_project_at_execute(tmp_path)
    runner.invoke(app, ["reopen", "brainstorm"])
    _ok(["decision", "add", "--text", "choice 4", "--rationale", "better",
         "--supersedes", "D-01"])

    result = _ok(["level", "full"])

    assert "D-01" not in result.output
    assert "D-04" in result.output


@pytest.mark.parametrize("target", ["fast", "quick"])
def test_level_refuses_to_stay_or_move_down(tmp_path, monkeypatch, target):
    monkeypatch.chdir(tmp_path)
    _ok(["init"])
    _ok(["new", "Thing", "--level", "fast"])
    before = (_project_dir(tmp_path) / "project.md").read_bytes()

    result = runner.invoke(app, ["level", target])

    assert result.exit_code != 0
    assert "only moves up" in result.output
    assert (_project_dir(tmp_path) / "project.md").read_bytes() == before


def test_level_refuses_an_unknown_level_naming_fast_and_full(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ok(["init"])
    _ok(["new", "Thing", "--level", "fast"])

    result = runner.invoke(app, ["level", "huge"])

    assert result.exit_code != 0
    assert "fast" in result.output and "full" in result.output


def test_level_on_a_full_project_refuses(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ok(["init"])
    _ok(["new", "Thing"])

    result = runner.invoke(app, ["level", "full"])

    assert result.exit_code != 0
    assert "only moves up" in result.output
