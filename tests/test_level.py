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


# --- quick to fast: the brief seeds the three documents ------------------------------


def _quick_project_with_brief(tmp_path, proof="ran it: ok\n", complete=True):
    _ok(["init"])
    _ok(["new", "Thing", "--level", "quick"])
    _ok(["section", "set", "brief", "Goal", "--stdin"], "Fix the help typo.\n")
    _ok(["section", "set", "brief", "Done when", "--stdin"], "- help shows 'workflow'\n")
    _ok(["section", "set", "brief", "Deferred", "--stdin"], "- reword the epilog\n- add an example\n")
    if proof is not None:
        _ok(["section", "set", "brief", "Proof", "--stdin"], proof)
        if complete:
            _ok(["advance"])


def test_level_fast_seeds_the_documents_from_the_brief(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project_with_brief(tmp_path)
    brief_before = (_project_dir(tmp_path) / "brief.md").read_bytes()

    _ok(["level", "fast"])

    project = _load(tmp_path)
    assert (project.level, project.phase, project.status) == ("fast", "brainstorm", "active")
    brainstorm = (_project_dir(tmp_path) / "brainstorm.md").read_text()
    understanding = brainstorm.split("## Current understanding", 1)[1].split("\n## ", 1)[0]
    for text in ("Fix the help typo.", "reword the epilog", "add an example"):
        assert text in understanding
    spec = (_project_dir(tmp_path) / "spec.md").read_text()
    assert "### REQ-01" in spec and "help shows 'workflow'" in spec.split("### REQ-01", 1)[1]
    plan_text = (_project_dir(tmp_path) / "plan.md").read_text()
    t01 = plan_text.split("### T-01", 1)[1]
    assert "Implements: REQ-01" in t01 and "Progress: done" in t01
    assert (_project_dir(tmp_path) / "brief.md").read_bytes() == brief_before


def test_level_fast_leaves_t01_pending_without_proof(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project_with_brief(tmp_path, proof=None)

    _ok(["level", "fast"])

    t01 = (_project_dir(tmp_path) / "plan.md").read_text().split("### T-01", 1)[1]
    assert "Progress: pending" in t01


def test_level_quick_to_full_equals_fast_then_full(tmp_path, monkeypatch):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    monkeypatch.chdir(one)
    _quick_project_with_brief(one)
    _ok(["level", "fast"])
    _ok(["level", "full"])
    monkeypatch.chdir(two)
    _quick_project_with_brief(two)

    _ok(["level", "full"])

    project = _load(two)
    assert (project.level, project.phase, project.status) == ("full", "brainstorm", "active")
    for name in ("brainstorm.md", "spec.md", "plan.md", "brief.md", "project.md"):
        assert (_project_dir(two) / name).read_bytes() == (_project_dir(one) / name).read_bytes(), name
    monkeypatch.chdir(tmp_path)


def test_level_fast_keeps_every_check_of_the_brief(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ok(["init"])
    _ok(["new", "Thing", "--level", "quick"])
    _ok(["section", "set", "brief", "Goal", "--stdin"], "Do two things.\n")
    _ok(["section", "set", "brief", "Done when", "--stdin"], "- check A\n- check B\n")

    _ok(["level", "fast"])

    spec = (_project_dir(tmp_path) / "spec.md").read_text()
    assert "### REQ-01 — check A" in spec and "### REQ-02 — check B" in spec
    brainstorm = (_project_dir(tmp_path) / "brainstorm.md").read_text()
    assert "check B" in brainstorm
    plan_text = (_project_dir(tmp_path) / "plan.md").read_text()
    assert "Implements: REQ-01" in plan_text and "### T-02" not in plan_text


def test_level_fast_on_an_empty_brief_seeds_no_requirement_or_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ok(["init"])
    _ok(["new", "Thing", "--level", "quick"])

    _ok(["level", "fast"])

    assert "### REQ-" not in (_project_dir(tmp_path) / "spec.md").read_text()
    assert "### T-" not in (_project_dir(tmp_path) / "plan.md").read_text()
    assert _load(tmp_path).level == "fast"
