"""`execution fan-out` points the orchestrator at the execute skill's Fan-out section.

The text form names the section and the skill; the linear switch does not,
and the JSON form is unchanged.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import config, projects
from specflo.cli import app

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    projects.switch_project(tmp_path, cfg, "thing")
    monkeypatch.chdir(tmp_path)


def test_fan_out_names_the_skill_section(project):
    result = runner.invoke(app, ["execution", "fan-out"])

    assert result.exit_code == 0, result.output
    assert "Fan-out" in result.output and "specflo-execute" in result.output


def test_an_unchanged_fan_out_still_points_at_it(project):
    runner.invoke(app, ["execution", "fan-out"])

    result = runner.invoke(app, ["execution", "fan-out"])

    assert "unchanged" in result.output and "specflo-execute" in result.output


def test_linear_does_not(project):
    runner.invoke(app, ["execution", "fan-out"])

    result = runner.invoke(app, ["execution", "linear"])

    assert result.exit_code == 0, result.output
    assert "Fan-out" not in result.output and "specflo-execute" not in result.output


def test_json_is_unchanged(project):
    result = runner.invoke(app, ["execution", "fan-out", "--json"])

    assert json.loads(result.output) == {"execution": "fan-out", "changed": True}
