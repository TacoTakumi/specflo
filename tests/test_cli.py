import json

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


def test_status_reports_the_active_project_as_json(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["active_project"] == "my-thing"
    assert data["name"] == "My Thing"
    assert data["phase"] == "brainstorm"
    assert data["next_phase"] == "spec"
    assert data["next_step"]
