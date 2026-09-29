"""Harden level: one phase, execute, worked from a brief of Scope, Focus and
Stop when, and a plan that starts empty and grows from review findings."""

import json
import re

import pytest
from typer.testing import CliRunner

from specflo import config, daemon, markdown, plan, projects, review, spec, workflow
from specflo.cli import app

runner = CliRunner()

SECTIONS = ["Scope", "Focus", "Stop when"]


def _ok(args, stdin=None):
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result


def _checkout(path, monkeypatch):
    path.mkdir()
    config.init_config(path)
    monkeypatch.chdir(path)
    return path


def _hosted_checkout(path, monkeypatch, live_daemon):
    """A checkout with the live daemon registered as remote ``home``."""
    _checkout(path, monkeypatch)
    _ok(["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]])
    return path


def _harden_project(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "local", monkeypatch)
    return root, _ok(["new", "Thing", "--level", "harden"])


def _headings(text):
    return [line[3:] for line in text.splitlines() if line.startswith("## ")]


def test_a_harden_project_starts_at_execute_with_a_brief_and_an_empty_plan(
    tmp_path, monkeypatch
):
    root, result = _harden_project(tmp_path, monkeypatch)

    cfg = config.load_config(root)
    project = projects.load_project(root, cfg, "thing")
    assert (project.level, project.phase) == ("harden", "execute")
    assert "thing/brief" in result.output and "thing/plan" in result.output
    names = {p.name for p in project.path.iterdir()}
    assert {"brief.md", "plan.md"} <= names
    assert not names & {"brainstorm.md", "spec.md"}
    assert plan.list_tasks(root, cfg, "thing") == []


def test_status_shows_level_harden_at_execute_and_no_other_phase(tmp_path, monkeypatch):
    _harden_project(tmp_path, monkeypatch)

    shown = _ok(["status"]).output
    info = json.loads(_ok(["status", "--json"]).output)

    assert "Phase:   execute" in shown
    assert "Level: harden" in shown
    assert (info["level"], info["phase"], info["next_phase"]) == ("harden", "execute", None)
    for phase in ("brainstorm", "spec"):
        assert phase not in info["next_step"]


def test_the_brief_has_scope_focus_and_stop_when_each_left_to_fill_in(tmp_path, monkeypatch):
    _harden_project(tmp_path, monkeypatch)

    shown = _ok(["doc", "show", "brief"]).output

    assert "\nlevel: harden\n" in shown
    assert _headings(shown) == SECTIONS
    for title in SECTIONS:
        body = markdown.section_body(shown, f"## {title}")
        assert "<!--" in body, title
        assert markdown.strip_comments(body).strip() == "", title


def test_task_list_on_the_empty_plan_names_no_task(tmp_path, monkeypatch):
    _harden_project(tmp_path, monkeypatch)

    assert "No tasks yet" in _ok(["task", "list"]).output


def test_harden_has_the_one_phase_execute():
    assert workflow.phases_for("harden") == ["execute"]


def test_new_with_an_unknown_level_names_harden_too(tmp_path, monkeypatch):
    _checkout(tmp_path / "local", monkeypatch)

    result = runner.invoke(app, ["new", "Thing", "--level", "huge"])

    assert result.exit_code != 0
    for level in ("quick", "fast", "full", "harden"):
        assert level in result.output


# The one status line that says where the project lives.
_WHERE_LINE = r"^(Dir:     .*|Remote:  .*)$"


def test_new_harden_on_a_remote_gives_the_same_project_through_the_daemon(
    tmp_path, monkeypatch, live_daemon
):
    views = {}
    for where, extra in (("local", []), ("hosted", ["--remote", "home"])):
        if extra:
            _hosted_checkout(tmp_path / where, monkeypatch, live_daemon)
        else:
            _checkout(tmp_path / where, monkeypatch)
        outputs = [_ok(["new", "Thing", "--level", "harden", *extra]).output]
        for args in (["status"], ["doc", "show", "brief"], ["doc", "show", "plan"],
                     ["task", "list"]):
            outputs.append(_ok(args).output)
        views[where] = [re.sub(_WHERE_LINE, "<where>", text, flags=re.MULTILINE)
                        for text in outputs]

    assert views["hosted"] == views["local"]
    assert "Level: harden" in views["hosted"][1]
    hosted = live_daemon["root"] / daemon.PROJECTS_DIRNAME / "thing"
    assert "\nlevel: harden\n" in (hosted / "project.md").read_text()
    assert (hosted / "brief.md").is_file() and (hosted / "plan.md").is_file()
    assert not (tmp_path / "hosted" / "docs" / "projects" / "thing").exists()
    assert config.hosted_projects(tmp_path / "hosted") == {"thing": "home"}


# --- validating the brief ---------------------------------------------------------


def _set(title, body):
    _ok(["section", "set", "brief", title, "--stdin"], body)


def _fill(scope="- src/specflo/\n", focus="none\n", stop="none\n"):
    for title, body in (("Scope", scope), ("Focus", focus), ("Stop when", stop)):
        if body is not None:
            _set(title, body)


def _issues():
    result = runner.invoke(app, ["validate", "brief", "--json"])
    assert result.exit_code == 1, result.output
    return json.loads(result.output)["issues"]


def test_validate_brief_on_the_scaffold_names_each_section_and_no_quick_one(
    tmp_path, monkeypatch
):
    _harden_project(tmp_path, monkeypatch)

    issues = _issues()

    for title in SECTIONS:
        assert any(issue.startswith(f"{title} is empty") for issue in issues), (title, issues)
    for title in ("Goal", "Done when", "Proof", "Deferred"):
        assert not any(title in issue for issue in issues), (title, issues)


def test_validate_brief_names_scope_while_it_holds_only_the_scaffold_comment(
    tmp_path, monkeypatch
):
    _harden_project(tmp_path, monkeypatch)
    _fill(scope=None)

    issues = _issues()

    assert len(issues) == 1 and issues[0].startswith("Scope is empty"), issues


def test_validate_brief_names_a_scope_that_says_none(tmp_path, monkeypatch):
    _harden_project(tmp_path, monkeypatch)
    _fill(scope="None.\n")

    issues = _issues()

    assert len(issues) == 1 and issues[0].startswith("Scope says none"), issues


def test_validate_brief_passes_with_a_scope_and_focus_and_stop_when_saying_none(
    tmp_path, monkeypatch
):
    _harden_project(tmp_path, monkeypatch)
    _fill()

    result = runner.invoke(app, ["validate", "brief"])

    assert result.exit_code == 0, result.output
    assert "ok - brief is ready." in result.output


def test_validate_brief_passes_with_text_in_every_section(tmp_path, monkeypatch):
    _harden_project(tmp_path, monkeypatch)
    _fill(scope="The whole repo.\n", focus="- error paths\n",
          stop="Two quiet harden rounds in a row.\n")

    assert _ok(["validate", "brief"]).exit_code == 0


@pytest.mark.parametrize("title", ["Focus", "Stop when"])
def test_validate_brief_names_focus_or_stop_when_left_as_the_scaffold_comment(
    tmp_path, monkeypatch, title
):
    _harden_project(tmp_path, monkeypatch)
    _fill(**{"focus" if title == "Focus" else "stop": None})

    issues = _issues()

    assert len(issues) == 1 and issues[0].startswith(f"{title} is empty"), issues
    assert "none" in issues[0]


@pytest.mark.parametrize("title", SECTIONS)
def test_validate_brief_names_a_section_that_is_absent(tmp_path, monkeypatch, title):
    root, _ = _harden_project(tmp_path, monkeypatch)
    _fill()
    path = projects.load_project(root, config.load_config(root), "thing").path / "brief.md"
    path.write_text(re.sub(
        rf"^## {title}\n.*?(?=^## |\Z)", "", path.read_text(), flags=re.S | re.M
    ))

    assert _issues() == [f"missing '{title}' section."]


def test_validate_brief_on_a_remote_harden_project_matches_a_local_one(
    tmp_path, monkeypatch, live_daemon
):
    views = {}
    for where, extra in (("local", []), ("hosted", ["--remote", "home"])):
        if extra:
            _hosted_checkout(tmp_path / where, monkeypatch, live_daemon)
        else:
            _checkout(tmp_path / where, monkeypatch)
        _ok(["new", "Thing", "--level", "harden", *extra])
        views[where] = [_issues()]
        _fill(scope=None)
        views[where].append(_issues())
        _fill(scope="- src/specflo/\n", focus=None, stop=None)
        views[where].append(_ok(["validate", "brief", "--json"]).output)

    assert views["hosted"] == views["local"]
    assert views["hosted"][1] == [views["hosted"][1][0]]
    assert views["hosted"][1][0].startswith("Scope is empty")
    assert json.loads(views["hosted"][2])["ready"] is True


# --- fix tasks only, with no task cap and no round budget --------------------------


def _files(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def _task(*extra):
    return runner.invoke(app, ["task", "add", "--text", "Harden the loader", "--acceptance",
                               "fixed", "--verify", "uv run pytest", *extra])


def _finding(severity="blocker", line=1):
    """`review finding add` in the open round; the new finding's ID."""
    added = _ok(["review", "finding", "add", "--severity", severity, "--at",
                 f"src/app.py:{line}", "--text", f"A defect on line {line}", "--json"])
    return json.loads(added.stdout)["id"]


def _fix(finding):
    """Add, start and finish a task fixing ``finding``; the task's ID."""
    task_id = _ok(["task", "add", "--text", f"Fix {finding}", "--acceptance", "fixed",
                   "--verify", "uv run pytest", "--fixes", finding]).output.split()[1]
    _ok(["task", "start", task_id])
    _ok(["task", "done", task_id])
    return task_id


def _round_asking_for_changes():
    """A gate round that checks each open item closed, raises one blocker and
    asks for changes, and a done task fixing that blocker; the blocker's ID."""
    for item in json.loads(_ok(["review", "start", "--json"]).stdout)["items"]:
        _ok(["review", "finding", "check", item, "closed"])
    finding = _finding()
    assert "changes-requested" in _ok(["review", "done"]).output
    _fix(finding)
    return finding


@pytest.mark.parametrize("extra", [[], ["--from", "REQ-01"]])
def test_task_add_without_fixes_is_refused_naming_fixes_and_changes_no_file(
    tmp_path, monkeypatch, extra
):
    root, _ = _harden_project(tmp_path, monkeypatch)
    project_dir = projects.load_project(root, config.load_config(root), "thing").path
    before = _files(project_dir)

    result = _task(*extra)

    assert result.exit_code == 1, result.output
    assert "harden level" in result.output and "--fixes F-NN" in result.output
    assert "Open items a task can fix: none." in result.output
    assert _files(project_dir) == before


def test_task_add_from_a_requirement_of_a_spec_is_still_refused_naming_fixes(
    tmp_path, monkeypatch
):
    root, _ = _harden_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    spec.start_spec(root, cfg, "thing")
    spec.add_requirement(root, cfg, "thing", "The loader reads the file", "It reads it")
    _ok(["review", "start"])
    finding = _finding()
    _ok(["review", "done"])
    project_dir = projects.load_project(root, cfg, "thing").path
    before = _files(project_dir)

    result = _task("--from", "REQ-01")

    assert result.exit_code == 1, result.output
    assert "harden level" in result.output and "--fixes F-NN" in result.output
    assert f"Open items a task can fix: {finding}." in result.output
    assert _files(project_dir) == before


def test_task_add_from_a_requirement_and_fixes_is_checked_against_the_spec(
    tmp_path, monkeypatch
):
    root, _ = _harden_project(tmp_path, monkeypatch)
    _ok(["review", "start"])
    finding = _finding()
    _ok(["review", "done"])
    project_dir = projects.load_project(root, config.load_config(root), "thing").path
    before = _files(project_dir)

    result = _task("--from", "REQ-01", "--fixes", finding)

    assert result.exit_code == 1, result.output
    assert "no spec.md" in result.output
    assert _files(project_dir) == before


def test_twenty_fix_tasks_raise_no_cap_warning(tmp_path, monkeypatch):
    root, _ = _harden_project(tmp_path, monkeypatch)
    _ok(["review", "start"])
    findings = [_finding("should-fix", line) for line in range(1, 21)]
    _ok(["review", "done"])

    for finding in findings:
        _ok(["task", "add", "--text", f"Fix {finding}", "--acceptance", "fixed",
             "--verify", "uv run pytest", "--fixes", finding])

    assert len(plan.list_tasks(root, config.load_config(root), "thing")) == 20
    warnings = json.loads(runner.invoke(app, ["validate", "plan", "--json"]).stdout)["warnings"]
    assert not [warning for warning in warnings if "level's cap" in warning], warnings
    assert json.loads(_ok(["checkpoint", "--json"]).stdout)["outgrew"] is None
    assert "level's cap" not in _ok(["status"]).output


def test_review_start_after_three_changes_requested_rounds_opens_without_over_budget(
    tmp_path, monkeypatch
):
    root, _ = _harden_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)

    for _ in range(3):
        _round_asking_for_changes()

    assert review.budget(root, cfg, "thing")["spent"] is False
    info = json.loads(_ok(["status", "--json"]).stdout)
    assert info["review"]["budget_spent"] is False
    assert "budget" not in info["next_step"]
    opened = json.loads(_ok(["review", "start", "--json"]).stdout)
    assert (opened["created"], opened["items"]) == (True, ["F-03"])
    assert review.round_kind(review.frontmatter(
        projects.load_project(root, cfg, "thing").path / "review-4.md"
    )) == review.GATE


def test_a_hosted_harden_project_takes_only_fix_tasks_and_has_no_round_budget(
    tmp_path, monkeypatch, live_daemon
):
    views = {}
    for where, extra in (("local", []), ("hosted", ["--remote", "home"])):
        if extra:
            _hosted_checkout(tmp_path / where, monkeypatch, live_daemon)
            project_dir = live_daemon["root"] / daemon.PROJECTS_DIRNAME / "thing"
        else:
            _checkout(tmp_path / where, monkeypatch)
            project_dir = tmp_path / where / "docs" / "projects" / "thing"
        _ok(["new", "Thing", "--level", "harden", *extra])
        before = _files(project_dir)
        refused = _task("--from", "REQ-01")
        assert refused.exit_code == 1, refused.output
        assert _files(project_dir) == before, where
        found = [_round_asking_for_changes() for _ in range(3)]
        info = json.loads(_ok(["status", "--json"]).stdout)
        opened = json.loads(_ok(["review", "start", "--json"]).stdout)
        assert (project_dir / "review-4.md").is_file(), where
        views[where] = [refused.output, found, info["review"]["budget_spent"],
                        info["next_step"], opened["created"], opened["items"]]

    assert views["hosted"] == views["local"]
    refusal, found, spent, next_step, created, items = views["hosted"]
    assert "harden level" in refusal and "--fixes F-NN" in refusal
    assert found == ["F-01", "F-02", "F-03"]
    assert (spent, created, items) == (False, True, ["F-03"])
    assert "budget" not in next_step
    assert not (tmp_path / "hosted" / "docs" / "projects" / "thing").exists()
