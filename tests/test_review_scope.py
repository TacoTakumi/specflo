"""`review scope "<line>"` writes the open round's Scope reviewed line.

The line replaces any line already there. The verb is refused, changing no
file, when no round is open or the text is empty or more than one line.
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


def _scope(path):
    return markdown.section_body(path.read_text(), review.SCOPE_HEADER).strip()


def test_scope_writes_the_one_line(round_file):
    result = runner.invoke(app, ["review", "scope", "all of the branch, src/ and tests/"])

    assert result.exit_code == 0, result.output
    assert _scope(round_file) == "all of the branch, src/ and tests/"


def test_a_second_call_replaces_it(round_file):
    runner.invoke(app, ["review", "scope", "first pass"])

    result = runner.invoke(app, ["review", "scope", "second pass"])

    assert result.exit_code == 0, result.output
    assert _scope(round_file) == "second pass"
    assert "first pass" not in round_file.read_text()


@pytest.mark.parametrize("value", ["", "   ", "one\ntwo"])
def test_an_empty_or_two_line_value_is_refused(round_file, value):
    before = round_file.read_bytes()

    result = runner.invoke(app, ["review", "scope", value])

    assert result.exit_code != 0
    assert round_file.read_bytes() == before


def test_no_open_round_is_refused(round_file):
    runner.invoke(app, ["review", "scope", "read it all"])
    closed = runner.invoke(app, ["review", "waive", "--reason", "by hand"])
    assert closed.exit_code == 0, closed.output
    before = round_file.read_bytes()

    result = runner.invoke(app, ["review", "scope", "late"])

    assert result.exit_code != 0 and "No review is open" in result.output
    assert round_file.read_bytes() == before


def test_the_guide_lists_the_verb():
    entries = {entry["name"]: entry for entry in guide.COMMANDS}
    assert entries["review scope"]["args"] == "<line>"
