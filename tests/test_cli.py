import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import config
from specflo.cli import app

runner = CliRunner()


@pytest.fixture
def cwd(monkeypatch, tmp_path):
    """Run each command with the tmp dir as the working directory."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_init_scaffolds_config_and_projects_dir(cwd):
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (cwd / ".specflo" / "config.yaml").is_file()
    assert (cwd / "docs" / "projects").is_dir()


def test_init_twice_fails(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["init"])
    assert result.exit_code != 0


def test_new_creates_project_and_makes_it_active(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["new", "My Thing"])
    assert result.exit_code == 0
    assert (cwd / "docs" / "projects" / "my-thing" / "project.md").is_file()
    assert config.load_config(cwd).active_project == "my-thing"


def test_new_without_init_fails(cwd):
    result = runner.invoke(app, ["new", "My Thing"])
    assert result.exit_code != 0


def test_status_without_init_is_friendly(cwd):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "init" in result.output.lower()

    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["initialized"] is False


def test_status_with_no_active_project(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "new" in result.output.lower()

    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["initialized"] is True
    assert data["active_project"] is None


def test_status_reports_the_active_project_for_humans(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "My Thing" in result.output
    assert "brainstorm" in result.output


def test_unknown_command_shows_full_help_not_just_a_hint():
    result = runner.invoke(app, ["bogus"])
    assert result.exit_code != 0
    out = result.output
    assert "bogus" in out  # names the offending command
    assert "Commands" in out  # the full help (command list) is shown
    assert "status" in out
    assert "--help' for help" not in out  # the bare "Try ... --help" hint is gone


def test_new_help_describes_the_name_argument():
    result = runner.invoke(app, ["new", "--help"])
    assert result.exit_code == 0
    assert "<name>" in result.output  # argument metavar
    assert "slug" in result.output.lower()  # argument is described


def test_top_level_help_shows_usage_examples():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Example" in result.output


def test_top_level_help_lists_new_with_its_argument():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "new <name>" in result.output


def test_top_level_help_does_not_add_args_to_argless_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # init and status take no positional arguments
    assert "init <" not in result.output
    assert "status <" not in result.output


def test_status_collapses_the_label_when_name_equals_slug(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "tpro"])  # name == slug == "tpro"
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Project: tpro" in result.output
    assert "tpro (tpro)" not in result.output


def test_status_shows_name_and_slug_when_they_differ(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["status"])
    assert "My Thing (my-thing)" in result.output


def test_status_shows_the_project_directory(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["status"])
    assert "docs/projects/my-thing" in result.output


def test_status_json_includes_the_project_directory(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["dir"].endswith("docs/projects/my-thing")
    assert Path(data["dir"]).is_absolute()


def test_status_reports_the_active_project_as_json(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["active_project"] == "my-thing"
    assert data["name"] == "My Thing"
    assert data["phase"] == "brainstorm"
    assert data["next_phase"] == "spec"
    assert data["next_step"]


def test_list_shows_projects_and_marks_the_active_one(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo"])  # Bravo is now active

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "bravo" in result.output
    # The active project (bravo) is marked; alpha is not.
    active_line = next(line for line in result.output.splitlines() if "bravo" in line)
    other_line = next(line for line in result.output.splitlines() if "alpha" in line)
    assert "*" in active_line
    assert "*" not in other_line


def test_list_shows_each_project_phase(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "brainstorm" in result.output


def test_list_without_projects_is_friendly(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "new" in result.output.lower()


def test_list_without_init_fails(cwd):
    result = runner.invoke(app, ["list"])
    assert result.exit_code != 0


def test_list_json_output(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo"])  # active

    data = json.loads(runner.invoke(app, ["list", "--json"]).output)
    assert data["active_project"] == "bravo"
    slugs = [p["slug"] for p in data["projects"]]
    assert slugs == ["alpha", "bravo"]
    by_slug = {p["slug"]: p for p in data["projects"]}
    assert by_slug["bravo"]["active"] is True
    assert by_slug["alpha"]["active"] is False
    assert by_slug["alpha"]["phase"] == "brainstorm"


def test_switch_changes_the_active_project(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo"])  # Bravo is active

    result = runner.invoke(app, ["switch", "Alpha"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert config.load_config(cwd).active_project == "alpha"


def test_switch_to_a_missing_project_fails(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["switch", "ghost"])
    assert result.exit_code != 0
    assert config.load_config(cwd).active_project is None


def test_switch_without_init_fails(cwd):
    result = runner.invoke(app, ["switch", "anything"])
    assert result.exit_code != 0


def test_top_level_help_lists_switch_with_its_argument():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "switch <name>" in result.output
