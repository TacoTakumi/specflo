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
