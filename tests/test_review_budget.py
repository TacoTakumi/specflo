"""The review round budget: how many rounds one level may take.

``review_max_rounds`` (default 2) counts the rounds of the project's current
level. When the latest round asks for changes and the level has used its
budget, ``review start`` opens nothing and names the two ways on: one more
round with ``--over-budget``, or a waive.
"""

from typer.testing import CliRunner

from specflo import config, projects, review
from specflo.cli import app

runner = CliRunner()


def _project(tmp_path, monkeypatch, level="full"):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-08-22", level=level)
    projects.switch_project(tmp_path, cfg, "Thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "docs" / "projects" / "thing"


def _round(project_dir, number, verdict, level="full", findings=("- F-0{n} (blocker) One",)):
    lines = "\n".join(f.format(n=number) for f in findings)
    level_line = "" if level is None else f"level: {level}\n"
    path = project_dir / f"review-{number}.md"
    path.write_text(
        f"---\nround: {number}\nverdict: {verdict}\ndate: '2026-08-22'\nsha: ''\n"
        f"{level_line}reason: ''\n---\n\n# Review round {number}\n\n## Findings\n\n{lines}\n"
    )
    return path


def _start(*args):
    return runner.invoke(app, ["review", "start", *args])


def test_start_past_the_budget_refuses_naming_both_ways_on(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested")
    _round(project_dir, 2, "changes-requested")

    result = _start()

    assert result.exit_code != 0
    assert "specflo review start --over-budget" in result.output
    assert "specflo review waive --reason" in result.output
    assert not (project_dir / "review-3.md").exists()


def test_over_budget_opens_one_more_round(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested")
    _round(project_dir, 2, "changes-requested")

    result = _start("--over-budget")

    assert result.exit_code == 0, result.output
    assert (project_dir / "review-3.md").is_file()


def test_each_further_round_needs_the_flag_again(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    for number in (1, 2, 3):
        _round(project_dir, number, "changes-requested")

    assert _start().exit_code != 0
    assert _start("--over-budget").exit_code == 0


def test_the_budget_does_not_limit_start_after_a_passing_round(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested")
    _round(project_dir, 2, "ready-to-merge", findings=("- none",))

    result = _start()

    assert result.exit_code == 0, result.output
    assert (project_dir / "review-3.md").is_file()


def test_the_budget_does_not_limit_start_after_a_waived_round(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested")
    _round(project_dir, 2, "changes-requested")
    _round(project_dir, 3, "waived", findings=("- none",))

    assert _start().exit_code == 0


def test_rounds_of_an_earlier_level_do_not_count(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch, level="full")
    _round(project_dir, 1, "changes-requested", level="fast")
    _round(project_dir, 2, "changes-requested", level="fast")

    result = _start()

    assert result.exit_code == 0, result.output
    assert review.frontmatter(project_dir / "review-3.md")["level"] == "full"


def test_a_round_with_no_level_counts_toward_the_current_level(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch, level="fast")
    _round(project_dir, 1, "changes-requested", level=None)
    _round(project_dir, 2, "changes-requested", level="fast")

    assert _start().exit_code != 0


def test_a_larger_budget_allows_more_rounds(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["config", "set", "review_max_rounds", "3"]).exit_code == 0
    _round(project_dir, 1, "changes-requested")
    _round(project_dir, 2, "changes-requested")

    assert _start().exit_code == 0
    assert (project_dir / "review-3.md").is_file()


def test_an_open_round_is_handed_back_whatever_the_budget(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested")
    _round(project_dir, 2, "changes-requested")
    assert _start("--over-budget").exit_code == 0

    again = _start()

    assert again.exit_code == 0, again.output
    assert "(already open)" in again.output


# --- review waive ----------------------------------------------------------------


def _review_issues(tmp_path):
    """The review gate's half of `validate execute`."""
    return review.completion_issues(tmp_path, config.load_config(tmp_path), "thing")


def test_waive_with_no_round_open_creates_one_closed_waived(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested")
    _round(project_dir, 2, "changes-requested")
    assert _review_issues(tmp_path)

    result = runner.invoke(app, ["review", "waive", "--reason", "x"])

    assert result.exit_code == 0, result.output
    assert "thing/review-3" in result.output
    fields = review.frontmatter(project_dir / "review-3.md")
    assert (fields["verdict"], fields["reason"]) == ("waived", "x")
    assert _review_issues(tmp_path) == []


def test_waive_with_a_round_open_closes_that_round(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    assert _start().exit_code == 0

    result = runner.invoke(app, ["review", "waive", "--reason", "not reviewing this one"])

    assert result.exit_code == 0, result.output
    fields = review.frontmatter(project_dir / "review-1.md")
    assert (fields["verdict"], fields["reason"]) == ("waived", "not reviewing this one")
    assert not (project_dir / "review-2.md").exists()


def test_waive_refuses_an_empty_reason_and_writes_nothing(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    _round(project_dir, 1, "changes-requested")

    for reason in ("", "  "):
        result = runner.invoke(app, ["review", "waive", "--reason", reason])
        assert result.exit_code != 0
        assert "reason" in result.output.lower()
    assert not (project_dir / "review-2.md").exists()

    assert _start().exit_code == 0
    before = (project_dir / "review-2.md").read_text()
    assert runner.invoke(app, ["review", "waive", "--reason", ""]).exit_code != 0
    assert (project_dir / "review-2.md").read_text() == before
