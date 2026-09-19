"""The egress verb: pinning an egress class on the active project."""

import json

from typer.testing import CliRunner

from specflo import config, projects
from specflo.cli import app

runner = CliRunner()


def _new_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["new", "Thing"]).exit_code == 0


def _egress_of(tmp_path, slug="thing"):
    root = config.find_root(tmp_path)
    return projects.load_project(root, config.load_config(root), slug).egress


def test_a_new_project_has_no_egress_pin(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)
    assert _egress_of(tmp_path) == ""
    assert "egress" not in (tmp_path / "docs" / "projects" / "thing" / "project.md").read_text()


def test_egress_verb_pins_each_class_on_the_active_project(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)
    for egress_class in ("local", "no-train", "open", "local"):
        result = runner.invoke(app, ["egress", egress_class])
        assert result.exit_code == 0, result.output
        assert result.output == f"Egress class for 'thing': {egress_class}\n"
        assert _egress_of(tmp_path) == egress_class


def test_the_project_record_shows_the_pinned_class(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)
    runner.invoke(app, ["egress", "local"])
    shown = runner.invoke(app, ["doc", "show", "project"])
    assert shown.exit_code == 0, shown.output
    assert "\negress: local\n" in shown.output


def test_egress_verb_reports_unchanged_when_the_class_matches(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)
    runner.invoke(app, ["egress", "local"])

    result = runner.invoke(app, ["egress", "local"])
    assert result.exit_code == 0
    assert "unchanged" in result.output
    assert _egress_of(tmp_path) == "local"


def test_egress_verb_json_emits_class_and_changed(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)

    first = runner.invoke(app, ["egress", "local", "--json"])
    assert first.exit_code == 0
    assert json.loads(first.output) == {"egress": "local", "changed": True}

    again = runner.invoke(app, ["egress", "local", "--json"])
    assert again.exit_code == 0
    assert json.loads(again.output) == {"egress": "local", "changed": False}


def test_egress_verb_refuses_an_unknown_class(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)
    runner.invoke(app, ["egress", "local"])

    result = runner.invoke(app, ["egress", "public"])
    assert result.exit_code != 0
    assert "'public'" in result.output
    assert all(name in result.output for name in ("local", "no-train", "open"))
    assert _egress_of(tmp_path) == "local"


def test_egress_verb_requires_an_active_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["egress", "local"])
    assert result.exit_code != 0


def test_the_pin_survives_shelve_resume_and_advance_through_the_cli(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)
    runner.invoke(app, ["egress", "local"])

    assert runner.invoke(app, ["shelve", "--reason", "later"]).exit_code == 0
    assert _egress_of(tmp_path) == "local"
    assert runner.invoke(app, ["resume", "thing"]).exit_code == 0
    assert _egress_of(tmp_path) == "local"

    root = config.find_root(tmp_path)
    projects.advance_project(root, config.load_config(root), "thing")
    assert _egress_of(tmp_path) == "local"
