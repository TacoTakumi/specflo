"""The reviewer brief: one set of rules for every review round.

``specflo review prompt`` prints what the reviewer of the open round needs:
the scope, what each severity means, what is not a finding, how to record,
that the CLI sets the verdict, and to run only the tests in scope.
"""

from typer.testing import CliRunner

from specflo import config, projects
from specflo.cli import app

runner = CliRunner()


def _project(tmp_path, monkeypatch):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-08-22")
    projects.switch_project(tmp_path, cfg, "Thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "docs" / "projects" / "thing"


def _prompt():
    result = runner.invoke(app, ["review", "prompt"])
    assert result.exit_code == 0, result.output
    return result.output


# What every brief carries, whatever the round's scope.
_ELEMENTS = (
    # the severities, with the rule for agent-facing wording
    "blocker", "should-fix", "nit", "agent-facing",
    # what is not a finding
    "did not introduce", "specflo followup add",
    # how to record
    "specflo review finding add", "specflo review finding check",
    "## Scope reviewed", "- none",
    # the verdict is the CLI's
    "specflo review done", "verdict",
    # targeted tests
    "only the tests",
)


def test_a_whole_branch_round_gets_the_whole_brief(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    text = _prompt()

    assert "whole branch" in text
    assert "review-1.md" in text
    for element in _ELEMENTS:
        assert element in text, element


def test_a_delta_round_gets_its_range_and_every_item_to_check(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    (project_dir / "review-1.md").write_text(
        "---\nround: 1\nverdict: changes-requested\ndate: '2026-08-22'\nsha: 'abc1234'\n"
        "reason: ''\n---\n\n# Review round 1\n\n## Findings\n\n"
        "- F-01 (blocker) One\n- F-02 (should-fix) Two\n- F-03 (nit) Three\n"
    )
    runner.invoke(app, ["review", "start"])

    text = _prompt()

    assert "abc1234..HEAD" in text
    assert "outside" in text                     # the range bounds what is a finding
    for item in ("F-01", "F-02"):
        assert item in text
    assert "F-03" not in text
    for element in _ELEMENTS:
        assert element in text, element


def test_no_open_round_refuses_naming_review_start(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["review", "prompt"])

    assert result.exit_code != 0
    assert "specflo review start" in result.output


def test_the_prompt_is_read_only_on_the_daemon():
    from specflo.daemon import routes

    assert "review_prompt" in routes.READ_OPERATIONS
