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
        assert not re.search(rf"\b{phase}\b", info["next_step"]), info["next_step"]


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


def _fix(finding, text=None):
    """Add, start and finish a task fixing ``finding``; the task's ID."""
    task_id = _ok(["task", "add", "--text", text or f"Fix {finding}", "--acceptance", "fixed",
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


# --- the level verb leaves a harden project where it is ----------------------------


def _tree(directory):
    """Every file under ``directory``, by its path relative to it, with its bytes."""
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in sorted(directory.rglob("*")) if path.is_file()}


def _refused(args):
    """``args`` run to a clean refusal: exit 1 with an error line, no traceback."""
    result = runner.invoke(app, args)
    assert result.exit_code == 1, (args, result.output)
    assert isinstance(result.exception, SystemExit), (args, result.exception)
    assert result.output.startswith("error: "), (args, result.output)
    return result.output


@pytest.mark.parametrize("target", ["quick", "fast", "full", "harden"])
def test_level_on_a_harden_project_is_refused_naming_harden_and_changes_no_file(
    tmp_path, monkeypatch, target
):
    root, _ = _harden_project(tmp_path, monkeypatch)
    projects_dir = projects.load_project(root, config.load_config(root), "thing").path.parent
    before = _tree(projects_dir)

    shown = _refused(["level", target])

    assert "'thing' is at harden level" in shown
    assert "stands apart from quick, fast and full" in shown
    assert "`specflo new`" in shown
    assert _tree(projects_dir) == before
    project = projects.load_project(root, config.load_config(root), "thing")
    assert (project.level, project.phase) == ("harden", "execute")


@pytest.mark.parametrize("start", ["quick", "fast", "full"])
def test_level_harden_is_refused_on_every_other_level_and_changes_no_file(
    tmp_path, monkeypatch, start
):
    root = _checkout(tmp_path / "local", monkeypatch)
    _ok(["new", "Thing", "--level", start])
    projects_dir = projects.load_project(root, config.load_config(root), "thing").path.parent
    before = _tree(projects_dir)

    shown = _refused(["level", "harden"])

    assert "Unknown level 'harden': expected one of 'quick', 'fast', 'full'." in shown
    assert _tree(projects_dir) == before


def test_level_on_a_hosted_harden_project_is_refused_as_on_a_local_one(
    tmp_path, monkeypatch, live_daemon
):
    views = {}
    for where, extra in (("local", []), ("hosted", ["--remote", "home"])):
        if extra:
            _hosted_checkout(tmp_path / where, monkeypatch, live_daemon)
            projects_dir = live_daemon["root"] / daemon.PROJECTS_DIRNAME
        else:
            _checkout(tmp_path / where, monkeypatch)
            projects_dir = tmp_path / where / "docs" / "projects"
        _ok(["new", "Thing", "--level", "harden", *extra])
        before = _tree(projects_dir)
        views[where] = [_refused(["level", target]) for target in ("fast", "full")]
        assert _tree(projects_dir) == before, where
        assert "Level: harden" in _ok(["status"]).output, where

    assert views["hosted"] == views["local"]
    for shown in views["hosted"]:
        assert "'thing' is at harden level" in shown and "`specflo new`" in shown


# --- completing on an empty ledger, not on a verdict -------------------------------


NO_HARDEN_ROUND = "no harden round closed hardened"


def _execute_issues():
    """`validate execute --json` run to a failure; its issues."""
    result = runner.invoke(app, ["validate", "execute", "--json"])
    assert result.exit_code == 1, result.output
    return json.loads(result.output)["issues"]


def _passing(root):
    return review.review_state(root, config.load_config(root), "thing")["passing"]


def _harden_round(*raised):
    """A harden round that checks each open item closed and raises a blocker
    for each of ``raised`` lines, or one nit with none, so it closes hardened;
    the IDs of what it raised."""
    for item in json.loads(_ok(["review", "start", "--harden", "--json"]).stdout)["items"]:
        _ok(["review", "finding", "check", item, "closed"])
    found = [_finding("blocker", line) for line in raised] or [_finding("nit")]
    assert "closed hardened" in _ok(["review", "done"]).output
    return found


def test_validate_execute_on_a_new_harden_project_names_the_brief_and_the_harden_round(
    tmp_path, monkeypatch
):
    _harden_project(tmp_path, monkeypatch)

    issues = _execute_issues()

    for title in SECTIONS:
        assert any(issue.startswith(f"{title} is empty") for issue in issues), (title, issues)
    assert issues[-1].startswith(NO_HARDEN_ROUND), issues
    assert "`specflo review start --harden`" in issues[-1]
    assert not [issue for issue in issues if "no tasks captured" in issue], issues


@pytest.mark.parametrize("rounds", ["none", "gate", "waived harden"])
def test_validate_execute_fails_with_no_harden_round_closed_hardened(
    tmp_path, monkeypatch, rounds
):
    root, _ = _harden_project(tmp_path, monkeypatch)
    _fill()
    if rounds == "gate":
        _ok(["review", "start"])
        _finding("nit")
        assert "closed ready-to-merge" in _ok(["review", "done"]).output
    elif rounds == "waived harden":
        _ok(["review", "start", "--harden"])
        _ok(["review", "waive", "--reason", "Not reviewed"])

    issues = _execute_issues()

    assert len(issues) == 1 and issues[0].startswith(NO_HARDEN_ROUND), issues
    assert "`specflo review start --harden`" in issues[0]
    if rounds != "none":
        assert _passing(root) is False


@pytest.mark.parametrize("flag", [["--harden"], []])
def test_validate_execute_fails_while_a_round_is_open(tmp_path, monkeypatch, flag):
    root, _ = _harden_project(tmp_path, monkeypatch)
    _fill()
    _harden_round()
    _ok(["review", "start", *flag])

    assert _execute_issues() == [
        "review round 2 is still open (review-2.md): close it with `specflo review done`."
    ]
    assert _passing(root) is False


@pytest.mark.parametrize("progress", ["pending", "in_progress"])
def test_validate_execute_fails_with_a_fix_task_not_done(tmp_path, monkeypatch, progress):
    _harden_project(tmp_path, monkeypatch)
    _fill()
    (finding,) = _harden_round(1)
    task_id = _ok(["task", "add", "--text", f"Fix {finding}", "--acceptance", "fixed",
                   "--verify", "uv run pytest", "--fixes", finding]).output.split()[1]
    if progress == "in_progress":
        _ok(["task", "start", task_id])

    issues = _execute_issues()

    assert len(issues) == 1 and issues[0].startswith(f"not all tasks are done: {task_id}"), issues


@pytest.mark.parametrize("after", ["fix", "waive"])
def test_validate_execute_fails_with_an_open_ledger_item(tmp_path, monkeypatch, after):
    root, _ = _harden_project(tmp_path, monkeypatch)
    _fill()
    (finding,) = _harden_round(1)
    _fix(finding)
    latest = "review-1.md"
    if after == "waive":
        # A waive covers what a gate round left open in a normal project;
        # here no round's verdict gates, so the item stays open.
        _ok(["review", "waive", "--reason", "Not reviewed"])
        latest = "review-2.md"

    issues = _execute_issues()

    assert len(issues) == 1, issues
    assert issues[0].startswith(
        f"{finding} is still open after the latest review round ({latest})"
    ), issues
    assert f"`specflo task add --fixes {finding}`" in issues[0]
    assert _passing(root) is False


def test_validate_execute_names_the_brief_once_the_rest_holds(tmp_path, monkeypatch):
    root, _ = _harden_project(tmp_path, monkeypatch)
    _fill(scope="None.\n")
    _harden_round()

    issues = _execute_issues()

    assert len(issues) == 1 and issues[0].startswith("Scope says none"), issues
    assert _passing(root) is True


def test_validate_execute_passes_on_an_empty_plan_after_a_clean_harden_round(
    tmp_path, monkeypatch
):
    root, _ = _harden_project(tmp_path, monkeypatch)
    _fill()
    _harden_round()

    assert plan.list_tasks(root, config.load_config(root), "thing") == []
    assert "ok - execute is ready." in _ok(["validate", "execute"]).output
    assert "ok - plan is ready." in _ok(["validate", "plan"]).output
    assert _passing(root) is True


def test_validate_execute_passes_after_a_changes_requested_gate_round_once_the_ledger_is_empty(
    tmp_path, monkeypatch
):
    root, _ = _harden_project(tmp_path, monkeypatch)
    cfg = config.load_config(root)
    _fill()
    _harden_round()
    _ok(["review", "start"])
    rejected, deferred, fixed = (_finding("blocker", line) for line in (1, 2, 3))
    assert "changes-requested" in _ok(["review", "done"]).output
    _ok(["review", "finding", "reject", rejected, "--reason", "The caller never passes None"])
    _ok(["review", "finding", "defer", deferred, "--do", "Name the file in the error"])
    _fix(fixed)
    _harden_round()

    kinds = [
        (review.round_kind(fields := review.frontmatter(path)), fields["verdict"])
        for _, path in review.round_files(root, cfg, "thing")
    ]
    assert kinds == [(review.HARDEN, review.HARDENED), (review.GATE, review.CHANGES_REQUESTED),
                     (review.HARDEN, review.HARDENED)]
    assert "ok - execute is ready." in _ok(["validate", "execute"]).output
    assert _passing(root) is True
    assert "latest round 3 hardened" in _ok(["status"]).output

    result = _ok(["advance"])

    assert "Completed project 'thing'." in result.output
    assert projects.load_project(root, config.load_config(root), "thing").status == "complete"


def test_completing_a_hosted_harden_project_matches_a_local_one(
    tmp_path, monkeypatch, live_daemon
):
    views = {}
    for where, extra in (("local", []), ("hosted", ["--remote", "home"])):
        if extra:
            _hosted_checkout(tmp_path / where, monkeypatch, live_daemon)
        else:
            _checkout(tmp_path / where, monkeypatch)
        _ok(["new", "Thing", "--level", "harden", *extra])
        _fill()
        views[where] = [_execute_issues()]
        (finding,) = _harden_round(1)
        views[where].append(_execute_issues())
        _fix(finding)
        _ok(["review", "start"])
        _ok(["review", "finding", "check", finding, "closed"])
        _finding("blocker", 2)
        _ok(["review", "done"])
        views[where].append(_execute_issues())
        _ok(["review", "finding", "reject", "F-02", "--reason", "Not a defect"])
        views[where].append(_ok(["validate", "execute"]).output)
        views[where].append(json.loads(_ok(["status", "--json"]).stdout)["review"]["passing"])
        views[where].append(_ok(["advance"]).output.splitlines()[0])
        views[where].append(json.loads(_ok(["status", "--json"]).stdout)["status"])

    assert views["hosted"] == views["local"]
    no_round, open_item, still_open, ready, passing, advanced, status = views["hosted"]
    assert len(no_round) == 1 and no_round[0].startswith(NO_HARDEN_ROUND)
    assert len(open_item) == 1 and open_item[0].startswith("F-01 is still open")
    assert len(still_open) == 1 and still_open[0].startswith("F-02 is still open")
    assert "ok - execute is ready." in ready
    assert passing is True
    assert advanced == "Completed project 'thing'."
    assert status == "complete"


# --- the next-step hint names the next move, one state at a time -------------------


TEST_COMMAND = "uv run pytest -q"

START_HARDEN_ROUND = (
    "No harden round has closed hardened yet - fill in the brief first if `specflo validate"
    " brief` names an issue, then open a harden round with `specflo review start --harden`,"
    " hand a fresh-context reviewer the brief `specflo review prompt` prints, and close the"
    " round with `specflo review done`."
)


def _next_hint():
    """The Next line `status` prints, without its label."""
    (line,) = [line for line in _ok(["status"]).output.splitlines() if line.startswith("Next:")]
    return line[len("Next:"):].strip()


def _open_round_hint(name):
    return (
        f"Finish the open review round {name}: run `specflo review start` to take it back"
        " (a round nobody wrote into yet takes HEAD), hand a fresh-context reviewer the"
        " brief `specflo review prompt` prints and have it record findings through the CLI;"
        " then close it with `specflo review done`."
    )


def _passing_hint():
    return (
        "No open item is left and a harden round closed hardened - run the whole test suite"
        f" (`{TEST_COMMAND}`) once, then run `specflo advance` to complete the project."
    )


def _stop_hint(first, second, finish):
    return (
        f"The last two harden rounds, {first} and {second}, raised no new blocker or"
        " should-fix finding, so hardening may stop here; that is the user's call. To stop,"
        f" run the whole test suite (`{TEST_COMMAND}`) once, then {finish}. To go on, open"
        " another harden round with `specflo review start --harden`."
    )


def _harden_checkout(tmp_path, monkeypatch):
    _harden_project(tmp_path, monkeypatch)
    _ok(["config", "set", "test_command", TEST_COMMAND])


def test_status_on_a_harden_project_names_the_next_move_one_state_at_a_time(
    tmp_path, monkeypatch
):
    _harden_checkout(tmp_path, monkeypatch)
    assert _next_hint() == START_HARDEN_ROUND
    _fill()
    assert _next_hint() == START_HARDEN_ROUND

    _ok(["review", "start", "--harden"])
    assert _next_hint() == _open_round_hint("review-1.md")

    finding = _finding()
    _ok(["review", "done"])
    assert _next_hint() == (
        f"{finding} is open with no fix task: add one with `specflo task add --fixes"
        f" {finding}`, work it to done and commit, then open the next harden round with"
        " `specflo review start --harden` to check it."
    )

    task_id = _ok(["task", "add", "--text", f"Fix {finding}", "--acceptance", "fixed",
                   "--verify", "uv run pytest", "--fixes", finding]).output.split()[1]
    work = (
        f"Work the next fix task: {task_id} (`specflo task show`); once every fix task is"
        " done and committed, open the next harden round with `specflo review start"
        " --harden` to check the fixes."
    )
    assert _next_hint() == work
    _ok(["task", "start", task_id])
    assert _next_hint() == work

    _ok(["task", "done", task_id])
    assert _next_hint() == (
        "Every open item has a done fix task - with the fixes committed, open the next"
        f" harden round with `specflo review start --harden` to check {finding}, hand a"
        " fresh-context reviewer the brief `specflo review prompt` prints, and close the"
        " round with `specflo review done`."
    )

    _harden_round()
    assert _next_hint() == _passing_hint()

    _harden_round()
    assert _next_hint() == _stop_hint(
        "review-2.md", "review-3.md", "run `specflo advance` to complete the project"
    )


def test_the_stop_suggestion_names_an_item_two_quiet_harden_rounds_left_open(
    tmp_path, monkeypatch
):
    _harden_checkout(tmp_path, monkeypatch)
    _fill()
    (finding,) = _harden_round(1)
    # Each round that checks the item open needs a new done fix task first.
    for attempt in range(2):
        _fix(finding, f"Fix {finding}, try {attempt + 1}")
        _ok(["review", "start", "--harden"])
        _ok(["review", "finding", "check", finding, "open"])
        _finding("nit")
        assert "closed hardened" in _ok(["review", "done"]).output

    assert _next_hint() == _stop_hint(
        "review-2.md", "review-3.md",
        f"fix {finding}, which is still open, and have a round opened with `specflo review"
        " start` check it closed, or reject or defer it; completion needs no open item"
        " (`specflo validate execute` names what is left)",
    )


def test_an_item_a_harden_round_checked_open_after_its_fix_asks_for_a_new_fix_task(
    tmp_path, monkeypatch
):
    _harden_checkout(tmp_path, monkeypatch)
    _fill()
    (finding,) = _harden_round(1)
    _fix(finding)
    _ok(["review", "start", "--harden"])
    _ok(["review", "finding", "check", finding, "open"])
    _finding("nit")
    assert "closed hardened" in _ok(["review", "done"]).output

    hint = _next_hint()
    assert "review-2.md" in hint and f"specflo task add --fixes {finding}" in hint, hint
    assert "no fix task" not in hint
    refused = runner.invoke(app, ["review", "start", "--harden"])
    assert refused.exit_code == 1
    assert "review-2.md checked it open after its fix" in refused.output
    assert "no fix task" not in refused.output


@pytest.mark.parametrize("rounds", ["gate", "waived harden"])
def test_status_asks_for_a_harden_round_while_none_closed_hardened(
    tmp_path, monkeypatch, rounds
):
    _harden_checkout(tmp_path, monkeypatch)
    _fill()
    if rounds == "gate":
        _ok(["review", "start"])
        _finding("nit")
        assert "closed ready-to-merge" in _ok(["review", "done"]).output
    else:
        _ok(["review", "start", "--harden"])
        _ok(["review", "waive", "--reason", "Not reviewed"])

    assert _next_hint() == START_HARDEN_ROUND


def test_status_on_a_hosted_harden_project_names_the_next_move_as_on_a_local_one(
    tmp_path, monkeypatch, live_daemon
):
    views = {}
    for where, extra in (("local", []), ("hosted", ["--remote", "home"])):
        if extra:
            _hosted_checkout(tmp_path / where, monkeypatch, live_daemon)
        else:
            _checkout(tmp_path / where, monkeypatch)
        _ok(["config", "set", "test_command", TEST_COMMAND])
        _ok(["new", "Thing", "--level", "harden", *extra])
        _fill()
        hints = [_next_hint()]
        _ok(["review", "start", "--harden"])
        found = [_finding("blocker", 1), _finding("should-fix", 2)]
        _ok(["review", "done"])
        hints.append(_next_hint())
        for finding in found:
            _fix(finding)
        hints.append(_next_hint())
        _harden_round()
        hints.append(_next_hint())
        views[where] = hints

    assert views["hosted"] == views["local"]
    start, add, next_round, passing = views["hosted"]
    assert start == START_HARDEN_ROUND
    assert add == (
        "F-01, F-02 are open with no fix task: add one for each with `specflo task add"
        " --fixes F-NN`, work each to done and commit, then open the next harden round with"
        " `specflo review start --harden` to check them."
    )
    assert next_round == (
        "Every open item has a done fix task - with the fixes committed, open the next"
        " harden round with `specflo review start --harden` to check F-01, F-02, hand a"
        " fresh-context reviewer the brief `specflo review prompt` prints, and close the"
        " round with `specflo review done`."
    )
    assert passing == _passing_hint()
