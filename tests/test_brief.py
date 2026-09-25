"""Quick level: one phase, execute, worked from one brief.md."""

from typer.testing import CliRunner

from specflo import config, projects, workflow
from specflo.cli import app

runner = CliRunner()


def _ok(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result


def _project_dir(tmp_path):
    return tmp_path / "docs" / "projects" / "thing"


def _quick_project(tmp_path):
    _ok(["init"])
    return _ok(["new", "Thing", "--level", "quick"])


def test_a_quick_project_starts_at_execute_with_a_brief_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _quick_project(tmp_path)

    project = projects.load_project(tmp_path, config.load_config(tmp_path), "thing")
    assert project.phase == "execute"
    assert "thing/brief" in result.output
    names = {p.name for p in _project_dir(tmp_path).iterdir()}
    assert "brief.md" in names
    assert not names & {"brainstorm.md", "spec.md", "plan.md"}
    headings = [
        line for line in (_project_dir(tmp_path) / "brief.md").read_text().splitlines()
        if line.startswith("## ")
    ]
    assert headings == ["## Goal", "## Done when", "## Proof", "## Deferred"]


def test_a_quick_project_has_nothing_to_reopen(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)

    result = runner.invoke(app, ["reopen"])

    assert result.exit_code != 0
    assert projects.load_project(
        tmp_path, config.load_config(tmp_path), "thing"
    ).phase == "execute"


def test_status_works_on_a_new_quick_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)

    result = _ok(["status"])

    assert "Phase:   execute" in result.output
    assert "Level: quick" in result.output


def test_the_phase_lists_per_level():
    assert workflow.phases_for("quick") == ["execute"]
    assert workflow.phases_for("fast") == workflow.PHASES
    assert workflow.phases_for("full") == workflow.PHASES


# --- writing and validating the brief ------------------------------------------


def _brief_text(tmp_path):
    return (_project_dir(tmp_path) / "brief.md").read_text()


def _section_of(text, title):
    lines = text.splitlines()
    start = lines.index(f"## {title}") + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def test_section_set_writes_each_brief_section_alone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)
    bodies = {
        "Goal": "Fix the typo in the help text.\n",
        "Done when": "- `specflo --help` shows 'workflow'.\n",
        "Proof": "specflo --help | grep workflow -> match\n",
        "Deferred": "- Reword the epilog.\n",
    }
    for title, body in bodies.items():
        before = _brief_text(tmp_path)
        _ok(["section", "set", "brief", title, "--stdin"], body)
        after = _brief_text(tmp_path)
        assert body.strip() in _section_of(after, title)
        for other in bodies:
            if other != title:
                assert _section_of(after, other) == _section_of(before, other)


def _fill(tmp_path, goal="Fix it.\n", done="- it works\n", proof="ran it: ok\n"):
    for title, body in (("Goal", goal), ("Done when", done), ("Proof", proof)):
        if body is not None:
            _ok(["section", "set", "brief", title, "--stdin"], body)


def test_validate_brief_passes_with_goal_one_check_and_proof(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)
    _fill(tmp_path)

    result = runner.invoke(app, ["validate", "brief"])

    assert result.exit_code == 0, result.output
    assert "ok - brief is ready." in result.output


def _issues(tmp_path):
    result = runner.invoke(app, ["validate", "brief", "--json"])
    assert result.exit_code == 1, result.output
    import json
    return json.loads(result.output)["issues"]


def test_validate_brief_names_an_empty_goal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)
    _fill(tmp_path, goal=None)
    assert any("Goal" in issue for issue in _issues(tmp_path))


def test_validate_brief_names_a_missing_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)
    _fill(tmp_path, done="Nothing listed here.\n")
    assert any("Done when" in issue for issue in _issues(tmp_path))


def test_validate_brief_names_two_checks_and_never_the_move_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)
    _fill(tmp_path, done="- it works\n- it is fast\n")
    issues = _issues(tmp_path)
    assert any("Done when" in issue and "2" in issue and "Deferred" in issue for issue in issues)
    assert not any("specflo level" in issue for issue in issues)


def test_validate_brief_names_an_empty_proof(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)
    _fill(tmp_path, proof=None)
    assert any("Proof" in issue for issue in _issues(tmp_path))


def test_validate_brief_ignores_an_empty_deferred_section(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)
    _fill(tmp_path)
    assert "## Deferred" in _brief_text(tmp_path)
    assert runner.invoke(app, ["validate", "brief"]).exit_code == 0


# --- completing a quick project --------------------------------------------------


def test_advance_refuses_a_quick_project_without_proof(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)
    _fill(tmp_path, proof=None)

    result = runner.invoke(app, ["advance"])

    assert result.exit_code != 0
    assert "Proof" in result.output
    assert projects.load_project(
        tmp_path, config.load_config(tmp_path), "thing"
    ).status == "active"


def test_advance_completes_a_quick_project_on_proof_without_a_review_round(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)
    _fill(tmp_path)

    result = _ok(["advance"])

    assert "Completed project 'thing'." in result.output
    assert projects.load_project(
        tmp_path, config.load_config(tmp_path), "thing"
    ).status == "complete"
    assert not list(_project_dir(tmp_path).glob("review-*.md"))


def test_the_checkpoint_of_a_quick_project_names_its_brief(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _quick_project(tmp_path)

    checkpoint_md = (_project_dir(tmp_path) / "checkpoint.md").read_text()
    read_first = checkpoint_md.split("## Read first", 1)[1].split("## ", 1)[0]
    assert "brief.md" in read_first
