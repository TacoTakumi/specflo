"""Quick level: one phase, execute, worked from one brief.md."""

from typer.testing import CliRunner

from specflo import config, projects, workflow
from specflo.cli import app

runner = CliRunner()


def _ok(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result


def _project_dir(tmp_path):
    return tmp_path / "docs" / "projects" / "thing"


def _quick_project(tmp_path):
    _ok(["init"])
    return _ok(["new", "Thing", "--level", "quick"])


def test_a_quick_project_starts_at_execute_with_a_brief_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _quick_project(tmp_path)

    project = projects.load_project(tmp_path, config.load_config(tmp_path), "thing")
    assert project.phase == "execute"
    assert "thing/brief" in result.output
    names = {p.name for p in _project_dir(tmp_path).iterdir()}
    assert "brief.md" in names
    assert not names & {"brainstorm.md", "spec.md", "plan.md"}
    headings = [
        line for line in (_project_dir(tmp_path) / "brief.md").read_text().splitlines()
        if line.startswith("## ")
    ]
    assert headings == ["## Goal", "## Done when", "## Proof", "## Deferred"]


def test_a_quick_project_has_nothing_to_reopen(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)

    result = runner.invoke(app, ["reopen"])

    assert result.exit_code != 0
    assert projects.load_project(
        tmp_path, config.load_config(tmp_path), "thing"
    ).phase == "execute"


def test_status_works_on_a_new_quick_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)

    result = _ok(["status"])

    assert "Phase:   execute" in result.output
    assert "Level: quick" in result.output


def test_the_phase_lists_per_level():
    assert workflow.phases_for("quick") == ["execute"]
    assert workflow.phases_for("fast") == workflow.PHASES
    assert workflow.phases_for("full") == workflow.PHASES
