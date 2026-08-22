"""Review rounds: the numbered ``review-N.md`` artifacts a project accrues.

A round is one end-of-execute whole-branch review. ``specflo review start``
mints the next one from a skeleton (REQ-01) and never reuses a number, so a
deleted round leaves a permanent gap (REQ-02).
"""

import yaml
from typer.testing import CliRunner

from specflo import config, projects
from specflo.cli import app

runner = CliRunner()


def _project(tmp_path, monkeypatch):
    """An active 'Thing' at ``tmp_path``, with the cwd moved into it."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-08-22")
    projects.switch_project(tmp_path, cfg, "Thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "docs" / "projects" / "thing"


def _frontmatter(path):
    return yaml.safe_load(path.read_text().split("---", 2)[1])


def _closed_round(project_dir, number, verdict="ready-to-merge"):
    path = project_dir / f"review-{number}.md"
    path.write_text(
        f"---\nround: {number}\nverdict: {verdict}\ndate: '2026-08-22'\n"
        f"sha: abc1234\nreason: ''\n---\n\n# Review round {number}\n"
    )
    return path


def test_mint_creates_the_first_round_and_prints_its_path(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["review", "start"])

    assert result.exit_code == 0, result.output
    minted = project_dir / "review-1.md"
    assert minted.is_file()
    assert str(minted) in result.output


def test_mint_writes_the_skeleton_frontmatter_and_headings(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)

    runner.invoke(app, ["review", "start"])

    minted = project_dir / "review-1.md"
    assert set(_frontmatter(minted)) == {"round", "verdict", "date", "sha", "reason"}
    assert _frontmatter(minted)["round"] == 1
    assert not _frontmatter(minted)["verdict"]        # open until `review done`
    body = minted.read_text()
    for heading in ("## Scope reviewed", "## Findings", "## Verdict"):
        assert heading in body


def test_mint_numbers_past_the_highest_round_and_never_fills_a_gap(
    tmp_path, monkeypatch
):
    # REQ-02: review-2.md was deleted; the next round is 4, not 2.
    project_dir = _project(tmp_path, monkeypatch)
    _closed_round(project_dir, 1)
    _closed_round(project_dir, 3, verdict="changes-requested")

    result = runner.invoke(app, ["review", "start"])

    assert result.exit_code == 0, result.output
    assert (project_dir / "review-4.md").is_file()
    assert not (project_dir / "review-2.md").exists()
    assert _frontmatter(project_dir / "review-4.md")["round"] == 4


def _open_round(project_dir, number):
    """An unclosed round: present, numbered, with no verdict yet."""
    path = project_dir / f"review-{number}.md"
    path.write_text(
        f"---\nround: {number}\nverdict: ''\ndate: '2026-08-22'\n"
        f"sha: ''\nreason: ''\n---\n\n# Review round {number}\n"
    )
    return path


def test_start_with_an_open_round_reuses_it_and_mints_nothing(tmp_path, monkeypatch):
    # REQ-03: a session that died mid-review is walked back into, not stepped
    # over -- there is no discard command, so reuse is the whole recovery path.
    project_dir = _project(tmp_path, monkeypatch)
    _closed_round(project_dir, 1)
    still_open = _open_round(project_dir, 2)
    before = still_open.read_text()

    result = runner.invoke(app, ["review", "start"])

    assert result.exit_code == 0, result.output
    assert str(still_open) in result.output
    assert "open" in result.output
    assert not (project_dir / "review-3.md").exists()
    assert still_open.read_text() == before          # reused, not rewritten


def test_open_round_is_the_one_with_an_empty_verdict(tmp_path, monkeypatch):
    # A closed latest round is no obstacle: the next `review start` mints.
    project_dir = _project(tmp_path, monkeypatch)
    _closed_round(project_dir, 1, verdict="changes-requested")

    result = runner.invoke(app, ["review", "start"])

    assert result.exit_code == 0, result.output
    assert (project_dir / "review-2.md").is_file()


def test_close_writes_the_verdict_into_the_open_round(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])
    minted = project_dir / "review-1.md"

    result = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert result.exit_code == 0, result.output
    assert _frontmatter(minted)["verdict"] == "ready-to-merge"
    assert "## Findings" in minted.read_text()       # the body survives the close


def test_close_accepts_each_of_the_three_verdicts(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    for number, verdict in enumerate(
        ("ready-to-merge", "changes-requested", "waived"), start=1
    ):
        runner.invoke(app, ["review", "start"])
        args = ["review", "done", "--verdict", verdict]
        if verdict == "waived":
            args += ["--reason", "not reviewing this one"]
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert _frontmatter(project_dir / f"review-{number}.md")["verdict"] == verdict


def test_close_with_no_open_round_refuses_and_changes_nothing(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    closed = _closed_round(project_dir, 1)
    before = closed.read_text()

    result = runner.invoke(app, ["review", "done", "--verdict", "changes-requested"])

    assert result.exit_code != 0
    assert "no review is open" in result.output.lower()
    assert closed.read_text() == before


def test_close_rejects_an_unknown_verdict_and_leaves_the_round_open(
    tmp_path, monkeypatch
):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(app, ["review", "done", "--verdict", "approved"])

    assert result.exit_code != 0
    for valid in ("ready-to-merge", "changes-requested", "waived"):
        assert valid in result.output
    assert not _frontmatter(project_dir / "review-1.md")["verdict"]


def test_waived_without_a_reason_is_refused_and_the_round_stays_open(
    tmp_path, monkeypatch
):
    # REQ-06: the recorded escape past the completion gate has to say why.
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(app, ["review", "done", "--verdict", "waived"])

    assert result.exit_code != 0
    assert "reason" in result.output.lower()
    assert not _frontmatter(project_dir / "review-1.md")["verdict"]


def test_waived_with_a_reason_closes_and_stores_it(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(
        app, ["review", "done", "--verdict", "waived", "--reason", "text"]
    )

    assert result.exit_code == 0, result.output
    fields = _frontmatter(project_dir / "review-1.md")
    assert (fields["verdict"], fields["reason"]) == ("waived", "text")


def test_waived_reason_is_not_required_by_the_other_verdicts(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert result.exit_code == 0, result.output
    assert not _frontmatter(project_dir / "review-1.md")["reason"]


def _git_repo(path):
    """Turn ``path`` into a git repo with one commit, and return its short sha."""
    import subprocess

    run = lambda *args: subprocess.run(  # noqa: E731 - terse local shorthand
        args, cwd=path, capture_output=True, text=True, check=True
    )
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    (path / "seed.txt").write_text("seed\n")
    run("git", "add", "seed.txt")
    run("git", "commit", "-qm", "seed")
    return run("git", "rev-parse", "--short", "HEAD").stdout.strip()


def test_stamp_records_todays_date_over_the_start_date(tmp_path, monkeypatch):
    import datetime

    from specflo import review

    project_dir = _project(tmp_path, monkeypatch)
    cfg = config.load_config(tmp_path)
    review.start_round(tmp_path, cfg, "thing", today="2020-01-01")

    result = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert result.exit_code == 0, result.output
    stamped = str(_frontmatter(project_dir / "review-1.md")["date"])
    assert stamped == datetime.date.today().isoformat()


def test_stamp_records_the_short_head_sha_inside_a_git_repo(tmp_path, monkeypatch):
    project_dir = _project(tmp_path, monkeypatch)
    sha = _git_repo(tmp_path)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert result.exit_code == 0, result.output
    assert _frontmatter(project_dir / "review-1.md")["sha"] == sha


def test_stamp_is_empty_outside_git_and_the_close_still_succeeds(
    tmp_path, monkeypatch
):
    # The git lookup degrades the way index.py's does: no repo, no stamp, no error.
    import datetime

    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(app, ["review", "done", "--verdict", "ready-to-merge"])

    assert result.exit_code == 0, result.output
    fields = _frontmatter(project_dir / "review-1.md")
    assert fields["sha"] == ""
    assert str(fields["date"]) == datetime.date.today().isoformat()


def test_ingest_replaces_an_untouched_skeleton_body_with_the_report(
    tmp_path, monkeypatch
):
    # REQ-10: the escape hatch for a subagent that returns its report as text.
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])
    report = tmp_path / "report.md"
    report.write_text("# Round 1\n\n## Findings\n\n- one nit.\n")

    result = runner.invoke(
        app,
        ["review", "done", "--verdict", "ready-to-merge", "--file", str(report)],
    )

    assert result.exit_code == 0, result.output
    minted = project_dir / "review-1.md"
    from specflo.review import body_of

    assert body_of(minted) == report.read_text()
    assert _frontmatter(minted)["verdict"] == "ready-to-merge"


def test_ingest_refuses_when_the_round_body_was_already_written(
    tmp_path, monkeypatch
):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])
    minted = project_dir / "review-1.md"
    minted.write_text(minted.read_text().replace("## Findings\n", "## Findings\n\n- a.\n"))
    before = minted.read_text()
    report = tmp_path / "report.md"
    report.write_text("a different report\n")

    result = runner.invoke(
        app,
        ["review", "done", "--verdict", "ready-to-merge", "--file", str(report)],
    )

    assert result.exit_code != 0
    assert "already has content" in result.output.lower()
    assert minted.read_text() == before          # still open, still the human's text


def test_ingest_of_a_missing_report_refuses_and_leaves_the_round_open(
    tmp_path, monkeypatch
):
    project_dir = _project(tmp_path, monkeypatch)
    runner.invoke(app, ["review", "start"])

    result = runner.invoke(
        app,
        ["review", "done", "--verdict", "waived", "--reason", "x",
         "--file", str(tmp_path / "nope.md")],
    )

    assert result.exit_code != 0
    assert not _frontmatter(project_dir / "review-1.md")["verdict"]
