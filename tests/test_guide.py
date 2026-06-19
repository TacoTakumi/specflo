import json

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from specflo import config, guide, projects
from specflo.cli import app

runner = CliRunner()


@pytest.fixture
def cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- build_guide: the three repo states ---------------------------------


def test_build_guide_uninitialized():
    data = guide.build_guide(None, None)
    assert data["initialized"] is False
    assert data["next_action"] == "init"
    assert data["pipeline"] == ["brainstorm", "spec", "plan", "execute"]
    assert data["commands"], "command table should not be empty"
    assert "phase" not in data


def test_build_guide_initialized_no_active(tmp_path):
    cfg = config.init_config(tmp_path)
    data = guide.build_guide(tmp_path, cfg)
    assert data["initialized"] is True
    assert data["active_project"] is None
    assert data["next_action"] == "new"
    assert "phase" not in data


def test_build_guide_active_project(tmp_path):
    cfg = config.init_config(tmp_path)
    project = projects.create_project(tmp_path, cfg, "My Thing")
    cfg.active_project = project.slug
    data = guide.build_guide(tmp_path, cfg)
    assert data["active_project"] == "my-thing"
    assert data["phase"] == "brainstorm"
    assert data["next_action"] == "brainstorm"
    assert data["next_step"], "active project should carry a next-step hint"


def test_build_guide_active_project_that_wont_load(tmp_path):
    # active_project set in config but the project dir is missing -> stays
    # useful, falls back to the "new" guidance rather than blowing up.
    cfg = config.init_config(tmp_path)
    cfg.active_project = "ghost"
    data = guide.build_guide(tmp_path, cfg)
    assert data["initialized"] is True
    assert data["next_action"] == "new"


# --- coverage guard: the table must list every CLI command --------------


def _leaf_paths(command, prefix=()):
    commands = getattr(command, "commands", None)
    if not commands:
        return [prefix]
    paths = []
    for name, sub in commands.items():
        paths.extend(_leaf_paths(sub, prefix + (name,)))
    return paths


def test_guide_table_covers_every_cli_command():
    cli = get_command(app)
    covered = {entry["name"] for entry in guide.COMMANDS}
    for path in _leaf_paths(cli):
        name = " ".join(path)
        assert name in covered, f"guide.COMMANDS is missing {name!r}"


# --- the CLI command: runs cold, in every state -------------------------


def test_guide_runs_cold_in_uninitialized_repo(cwd):
    result = runner.invoke(app, ["guide"])
    assert result.exit_code == 0
    out = result.output.lower()
    assert "init" in out  # tells you how to start
    for phase in ("brainstorm", "spec", "plan", "execute"):
        assert phase in out  # the pipeline is shown


def test_guide_json_uninitialized(cwd):
    data = json.loads(runner.invoke(app, ["guide", "--json"]).output)
    assert data["initialized"] is False
    assert data["next_action"] == "init"
    assert data["pipeline"][0] == "brainstorm"
    assert any(c["name"] == "advance" for c in data["commands"])


def test_guide_shows_you_are_here_for_active_project(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["guide"])
    assert result.exit_code == 0
    assert "my-thing" in result.output
    assert "next" in result.output.lower()

    data = json.loads(runner.invoke(app, ["guide", "--json"]).output)
    assert data["active_project"] == "my-thing"
    assert data["next_action"] == "brainstorm"
