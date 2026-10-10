"""`review clean` records a round with no findings.

It writes `- none` as the only line under Findings, so `review done` closes
the round ready-to-merge with no hand edit. A round that records a finding
already is refused and its file is left as it was. The guide lists the verb.
"""

import pytest
from typer.testing import CliRunner

from specflo import guide, markdown, review
from specflo.cli import app
from test_review_location import _project

runner = CliRunner()


@pytest.fixture
def round_file(tmp_path, monkeypatch):
    return _project(tmp_path, monkeypatch, repo=False)


def test_clean_then_done_closes_ready_to_merge(round_file):
    assert runner.invoke(app, ["review", "scope", "the whole branch"]).exit_code == 0

    result = runner.invoke(app, ["review", "clean"])

    assert result.exit_code == 0, result.output
    body = markdown.section_body(round_file.read_text(), review.FINDINGS_HEADER)
    assert body.strip() == "- none"
    done = runner.invoke(app, ["review", "done"])
    assert done.exit_code == 0, done.output
    assert review.frontmatter(round_file)["verdict"] == "ready-to-merge"


def test_clean_twice_keeps_one_line(round_file):
    runner.invoke(app, ["review", "clean"])

    assert runner.invoke(app, ["review", "clean"]).exit_code == 0

    body = markdown.section_body(round_file.read_text(), review.FINDINGS_HEADER)
    assert body.strip() == "- none"


def test_a_round_with_a_finding_is_refused(round_file):
    added = runner.invoke(app, ["review", "finding", "add", "--severity", "nit",
                                "--text", "A name reads oddly"])
    assert added.exit_code == 0, added.output
    before = round_file.read_bytes()

    result = runner.invoke(app, ["review", "clean"])

    assert result.exit_code != 0 and "F-01" in result.output
    assert round_file.read_bytes() == before


def test_no_open_round_is_refused(round_file):
    assert runner.invoke(app, ["review", "waive", "--reason", "by hand"]).exit_code == 0
    before = round_file.read_bytes()

    result = runner.invoke(app, ["review", "clean"])

    assert result.exit_code != 0 and "No review is open" in result.output
    assert round_file.read_bytes() == before


def test_the_guide_lists_the_verb():
    assert "review clean" in {entry["name"] for entry in guide.COMMANDS}
