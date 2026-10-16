"""`specflo list` names each project's level beside its phase.

The level says how much ceremony a project gets, so the listing tells a quick
fix, a research project and a full build apart at a glance; `--json` carries
it as `level`.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import config, projects
from specflo.cli import app

runner = CliRunner()


def _ok(args):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, (args, result.output)
    return result


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ok(["init"])
    return tmp_path


@pytest.mark.parametrize("level", projects.NEW_LEVELS)
def test_list_names_the_level_of_each_project_beside_its_phase(checkout, level):
    _ok(["new", "Thing", "--level", level])
    phase = projects.load_project(checkout, config.load_config(checkout), "thing").phase

    assert _ok(["list"]).output.splitlines() == [f"* thing  ({phase}, {level})"]

    (entry,) = json.loads(_ok(["list", "--json"]).output)["projects"]
    assert (entry["slug"], entry["phase"], entry["level"]) == ("thing", phase, level)


def test_list_tells_a_research_project_from_a_full_one(checkout):
    _ok(["new", "Alpha"])
    _ok(["new", "Bravo", "--level", "research"])

    assert _ok(["list"]).output.splitlines() == [
        "  alpha  (brainstorm, full)",
        "* bravo  (research, research)",
    ]
    data = json.loads(_ok(["list", "--json"]).output)
    assert [(p["slug"], p["phase"], p["level"]) for p in data["projects"]] == [
        ("alpha", "brainstorm", "full"),
        ("bravo", "research", "research"),
    ]


def test_list_keeps_the_level_ahead_of_the_status_marker(checkout):
    _ok(["new", "Alpha", "--level", "quick"])
    _ok(["shelve", "--reason", "later"])

    assert _ok(["list"]).output.splitlines() == ["* alpha  (execute, quick)  [shelved]: later"]


def test_list_json_carries_no_level_for_an_unreachable_hosted_project(checkout):
    _ok(["new", "Alpha"])
    _ok(["remote", "add", "dead", "http://127.0.0.1:9", "--token", "x"])
    config.record_hosted_project(checkout, "ghost", "dead")

    result = _ok(["list", "--json"])

    ghost = next(p for p in json.loads(result.output)["projects"] if p["slug"] == "ghost")
    assert ghost["phase"] is None and ghost["level"] is None
