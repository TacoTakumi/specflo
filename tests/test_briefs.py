"""Briefs inside a running project: the ask, facts, decisions and contract for one feature."""

import json
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


# --- tasks that cite a brief ---------------------------------------------------


def _plan_text(tmp_path):
    return (tmp_path / "docs" / "projects" / "thing" / "plan.md").read_text()


def test_task_add_from_a_brief_needs_no_requirement(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)
    _ok(["brief", "add", "Feedback form"])

    result = _ok(["task", "add", "--text", "add the form", "--acceptance", "form posts",
                  "--verify", "true", "--from", "B-01"])

    assert result.output.strip() == "Recorded T-02 (implements B-01)."
    assert "- Implements: B-01\n" in _plan_text(tmp_path)
    assert _ok(["validate", "plan"]).output.startswith("ok")


def test_task_add_from_an_unknown_brief_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)

    result = runner.invoke(app, ["task", "add", "--text", "add the form", "--acceptance", "x",
                                 "--verify", "true", "--from", "B-09"])

    assert result.exit_code != 0
    assert "B-09" in result.output and "specflo brief add" in result.output
    assert "T-02" not in _plan_text(tmp_path)


def test_task_show_prints_the_cited_brief_between_the_task_and_the_constraints(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)
    _ok(["section", "set", "plan", "Global constraints", "--stdin"], stdin="- Flutter 3.")
    _ok(["brief", "add", "Feedback form"])
    _ok(["brief", "set", "B-01", "Facts", "--stdin"], stdin="- the RN app posts to /feedback")
    _ok(["task", "add", "--text", "add the form", "--acceptance", "form posts",
         "--verify", "true", "--from", "B-01"])

    shown = _ok(["task", "show", "T-02"]).output

    task_block = shown.index("Acceptance: form posts")
    brief_heading = shown.index("# B-01 - Feedback form")
    facts = shown.index("- the RN app posts to /feedback")
    constraints = shown.index("## Global constraints")
    assert task_block < brief_heading < facts < constraints
    assert "sha:" not in shown

    as_json = json.loads(_ok(["task", "show", "T-02", "--json"]).output)
    assert as_json["briefs"][0]["id"] == "B-01"
    assert "- the RN app posts to /feedback" in as_json["briefs"][0]["body"]
    plain = json.loads(_ok(["task", "show", "T-01", "--json"]).output)
    assert plain["briefs"] == []
    assert "B-01" not in _ok(["task", "show", "T-01"]).output


# --- decisions in a brief -----------------------------------------------------


def test_decision_add_into_a_brief_and_decision_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)                     # D-01 Use SQLite
    _ok(["brief", "add", "Feedback form"])

    added = _ok(["decision", "add", "--brief", "B-01", "--diverges",
                 "--text", "No tablet layout", "--rationale", "no tablet users"])
    listed = _ok(["decision", "list"]).output
    only_divergences = _ok(["decision", "list", "--diverges"]).output

    assert added.output.strip() == "Recorded D-02 in B-01. Diverges from the reference design."
    brief_text = (_briefs_dir(tmp_path) / "B-01-feedback-form.md").read_text()
    assert "### D-02 — No tablet layout\n- Rationale: no tablet users\n- Diverges: yes\n" in brief_text
    assert listed.splitlines() == [
        "D-01  brainstorm  Use SQLite",
        "D-02  B-01        diverges  No tablet layout",
    ]
    assert only_divergences.splitlines() == ["D-02  B-01  diverges  No tablet layout"]


def test_decision_list_json_and_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project_at_execute(runner, app, tmp_path)
    _ok(["brief", "add", "Feedback form"])
    _ok(["decision", "add", "--brief", "B-01", "--text", "Post to /feedback"])        # D-02
    _ok(["decision", "add", "--text", "Post to /api/feedback", "--supersedes", "D-02"])  # D-03

    active = json.loads(_ok(["decision", "list", "--json"]).output)
    everything = _ok(["decision", "list", "--all"]).output

    assert [(d["id"], d["source"], d["diverges"]) for d in active] == [
        ("D-01", "brainstorm", False), ("D-03", "brainstorm", False),
    ]
    assert "D-02  B-01        Post to /feedback  [superseded by D-03]" in everything
    assert "No divergences recorded." in _ok(["decision", "list", "--diverges"]).output
