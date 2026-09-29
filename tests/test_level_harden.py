"""Harden level: one phase, execute, worked from a brief of Scope, Focus and
Stop when, and a plan that starts empty and grows from review findings."""

import json
import re

from typer.testing import CliRunner

from specflo import config, daemon, markdown, plan, projects, workflow
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
