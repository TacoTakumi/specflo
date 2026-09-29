"""A harden project is attended work: hardening stops on the user's say, so
`specflo auto` and the ladder refuse it, and the refusal changes no file."""

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from specflo import auto, cli
from specflo.cli import app
from specflo.errors import SpecfloError

runner = CliRunner()


def _ok(args):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, (args, result.output)
    return result


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repository with one commit and an active harden project, as the cwd."""
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Tester")
    (tmp_path / "app.txt").write_text("hello\n")
    _ok(["init"])
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    _ok(["new", "Thing", "--level", "harden"])
    return tmp_path


def _files(repo):
    """Every file outside .git, with its bytes and its modification time."""
    return {
        path.relative_to(repo).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }


def _refs(repo):
    return git(repo, "for-each-ref", "--format=%(refname) %(objectname)")


@pytest.mark.parametrize(
    "args",
    [
        ["auto"],
        ["auto", "--json"],
        ["auto", "--autonomy", "yolo", "--max-passes", "5"],
        ["auto", "--on"],
        ["auto", "--off"],
    ],
)
def test_auto_refuses_a_harden_project_naming_its_level_and_changes_no_file(repo, args):
    before = _files(repo)

    result = runner.invoke(app, args)

    assert result.exit_code != 0
    assert isinstance(result.exception, SpecfloError)
    assert "harden level" in str(result.exception)
    assert result.output == ""
    assert _files(repo) == before


def test_the_refusal_reaches_the_user_as_an_error_with_exit_1(repo, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_notify_stale_skills", lambda: None)
    monkeypatch.setattr(sys, "argv", ["specflo", "auto", "--json"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    shown = capsys.readouterr()
    assert exc.value.code == 1
    assert shown.out == ""
    assert shown.err.startswith("error: ") and "harden level" in shown.err


def test_the_ladder_refuses_a_harden_project_as_it_refuses_any_level_but_quick(repo):
    files, refs = _files(repo), _refs(repo)
    head = git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    result = runner.invoke(app, ["auto", "--ladder", "--json"])

    assert result.exit_code != 0
    assert "A ladder starts at quick level" in result.output
    assert "'harden'" in result.output
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == head
    assert _refs(repo) == refs
    assert _files(repo) == files


def test_the_auto_payload_functions_refuse_a_harden_project_too(repo):
    before = _files(repo)

    for emit in (auto.auto_text, auto.auto_pass_result, auto.auto_pass):
        with pytest.raises(SpecfloError, match="harden level"):
            emit(repo)

    assert _files(repo) == before


def test_after_the_refusals_no_auto_run_is_under_way(repo):
    for args in (["auto", "--on"], ["auto"], ["auto", "--ladder"]):
        assert runner.invoke(app, args).exit_code != 0

    info = json.loads(_ok(["status", "--json"]).output)

    assert info["auto_run"] == {"under_way": False}


# --- the continuation never offers auto on a harden project ------------------


def _done_fix_output(*flags):
    """Raise a blocker in a harden round, then add, start and finish a task
    fixing it; the output of `task done` with ``flags``."""
    _ok(["review", "start", "--harden"])
    _ok(["review", "finding", "add", "--severity", "blocker", "--at", "app.txt:1",
         "--text", "The greeting is wrong"])
    _ok(["review", "done"])
    _ok(["task", "add", "--text", "Fix the greeting", "--acceptance", "fixed",
         "--verify", "uv run pytest", "--fixes", "F-01"])
    _ok(["task", "start", "T-01"])
    return _ok(["task", "done", "T-01", *flags]).output


def test_task_done_on_a_harden_project_resumes_with_the_checkpoint_alone(repo):
    output = _done_fix_output()

    assert "You may clear context now - resume with `specflo checkpoint`." in output
    assert "specflo auto" not in output


def test_task_done_json_on_a_harden_project_resumes_with_the_checkpoint_alone(repo):
    data = json.loads(_done_fix_output("--json"))

    assert "You may clear context now" in data["continuation"]
    assert "specflo auto" not in data["continuation"]


def test_task_done_on_a_full_project_still_offers_auto(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ok(["init"])
    _ok(["new", "Thing"])
    _ok(["spec", "start"])
    _ok(["requirement", "add", "--text", "It builds", "--acceptance", "built"])
    _ok(["plan", "start"])
    _ok(["task", "add", "--text", "Build it", "--acceptance", "built", "--verify", "true",
         "--from", "REQ-01"])
    _ok(["task", "start", "T-01"])

    assert "(or `specflo auto` in an auto run)" in _ok(["task", "done", "T-01"]).output


def test_task_done_fallback_on_a_harden_project_does_not_offer_auto(repo, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("no checkpoint")

    monkeypatch.setattr(cli, "_client_checkpoint", fail)
    output = _done_fix_output()

    assert "You may clear context now - resume with `specflo checkpoint`." in output
    assert "specflo auto" not in output
