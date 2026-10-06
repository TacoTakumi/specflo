"""Briefs inside a running project: the ask, facts, decisions and contract for one feature."""

import subprocess

from typer.testing import CliRunner

from specflo import config, projects
from specflo.cli import app
from specflo.service import wire
from test_cli import _project_at_execute

runner = CliRunner()


def _ok(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result


def _briefs_dir(tmp_path):
    return tmp_path / "docs" / "projects" / "thing" / "briefs"


def _git(tmp_path, *args):
    return subprocess.run(
        ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo_with_a_commit(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "start")
    return _git(tmp_path, "rev-parse", "--short", "HEAD")


def _headings(path):
    return [line for line in path.read_text().splitlines() if line.startswith("## ")]


def test_brief_add_creates_the_file_with_its_sections_and_start_commit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)
    sha = _repo_with_a_commit(tmp_path)

    result = _ok(["brief", "add", "Feedback form"])

    path = _briefs_dir(tmp_path) / "B-01-feedback-form.md"
    assert path.is_file()
    assert result.output.splitlines()[0] == "thing/briefs/B-01"
    assert f"Recorded B-01 at {sha}." in result.output
    text = path.read_text()
    assert _headings(path) == ["## Ask", "## Facts", "## Decisions", "## Contract", "## Deferred"]
    assert f"\nsha: {sha}\n" in text
    assert "brief: B-01\n" in text
    assert "# B-01 - Feedback form" in text
    project = projects.load_project(tmp_path, config.load_config(tmp_path), "thing")
    assert (project.level, project.phase) == ("full", "execute")


def test_a_second_brief_mints_the_next_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)
    _ok(["brief", "add", "Feedback form"])

    result = _ok(["brief", "add", "Help sheet", "--json"])

    assert '"id": "B-02"' in result.output
    assert '"locator": "thing/briefs/B-02"' in result.output
    assert (_briefs_dir(tmp_path) / "B-02-help-sheet.md").is_file()


def test_brief_add_without_git_records_an_empty_start(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)

    result = _ok(["brief", "add", "Feedback form"])

    assert "Recorded B-01." in result.output
    assert "\nsha: \n" in (_briefs_dir(tmp_path) / "B-01-feedback-form.md").read_text()


def test_brief_add_needs_a_one_line_title(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)

    result = runner.invoke(app, ["brief", "add", "two\nlines"])

    assert result.exit_code != 0
    assert not _briefs_dir(tmp_path).exists()


def test_brief_set_replaces_one_section_and_bumps_updated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)
    _ok(["brief", "add", "Feedback form"])
    path = _briefs_dir(tmp_path) / "B-01-feedback-form.md"
    before = path.read_text().replace("updated: ", "updated: 2000-01-01", 1)
    path.write_text(before)

    result = _ok(["brief", "set", "B-01", "Facts", "--stdin"], stdin="- the form posts to /feedback\n")

    assert result.output.strip() == "Set 'Facts' in thing/briefs/B-01."
    after = path.read_text()
    assert "## Facts\n\n- the form posts to /feedback\n\n## Decisions" in after
    assert "<!-- what recon found" not in after
    assert "<!-- the user's words" in after
    assert "updated: 2000-01-01" not in after


def test_brief_set_reads_a_file_and_matches_the_section_case_free(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)
    _ok(["brief", "add", "Feedback form"])
    body = tmp_path / "ask.md"
    body.write_text("Rob, 2026-10-06: a feedback form in the app.\n")

    _ok(["brief", "set", "B-01", "ask", "--file", str(body)])

    text = (_briefs_dir(tmp_path) / "B-01-feedback-form.md").read_text()
    assert "## Ask\n\nRob, 2026-10-06: a feedback form in the app.\n" in text


def test_brief_set_refuses_the_decisions_section(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)
    _ok(["brief", "add", "Feedback form"])

    result = runner.invoke(app, ["brief", "set", "B-01", "Decisions", "--stdin"], input="D-09")

    assert result.exit_code != 0
    assert "specflo decision add --brief B-01" in result.output
    assert "D-09" not in (_briefs_dir(tmp_path) / "B-01-feedback-form.md").read_text()


def test_brief_set_refuses_an_unknown_section_or_brief(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)
    _ok(["brief", "add", "Feedback form"])

    unknown_section = runner.invoke(app, ["brief", "set", "B-01", "Goal", "--stdin"], input="x")
    unknown_brief = runner.invoke(app, ["brief", "set", "B-09", "Facts", "--stdin"], input="x")
    no_body = runner.invoke(app, ["brief", "set", "B-01", "Facts"])

    assert unknown_section.exit_code != 0 and "'Ask', 'Facts', 'Contract', 'Deferred'" in unknown_section.output
    assert unknown_brief.exit_code != 0 and "No brief B-09" in unknown_brief.output
    assert no_body.exit_code != 0 and "--file" in no_body.output


def test_the_brief_verbs_are_service_operations():
    assert wire.OPERATIONS["add_brief"].slug_scoped
    assert wire.OPERATIONS["set_brief_section"].slug_scoped
