"""Fast level finishes the way full level does: every task done, then a review."""

from typer.testing import CliRunner

import reviewhelp
from specflo import config, projects
from specflo.cli import app
from test_level import _fast_project_at_execute

runner = CliRunner()


def _full_level_review_message(tmp_path):
    """What advance says with every task done and no round, at full level."""
    path = tmp_path / "docs" / "projects" / "thing" / "project.md"
    path.write_text(path.read_text().replace("level: fast", "level: full"))
    message = runner.invoke(app, ["advance"]).output
    path.write_text(path.read_text().replace("level: full", "level: fast"))
    return message


def test_fast_advance_needs_a_review_round_like_full(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _fast_project_at_execute(tmp_path)

    result = runner.invoke(app, ["advance"])

    assert result.exit_code != 0
    assert result.output == _full_level_review_message(tmp_path)
    assert "review" in result.output
    assert projects.load_project(
        tmp_path, config.load_config(tmp_path), "thing"
    ).status == "active"


def test_fast_advance_completes_after_a_passing_review_round(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _fast_project_at_execute(tmp_path)
    runner.invoke(app, ["review", "start"])
    reviewhelp.review_done(runner, app, "ready-to-merge")

    result = runner.invoke(app, ["advance"])

    assert result.exit_code == 0, result.output
    assert "Completed project 'thing'." in result.output
