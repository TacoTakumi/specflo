"""A research project is attended work: a research session is a conversation
with the user at the keyboard, so `specflo auto` refuses it as it refuses a
harden project, the refusal changes no file, and no continuation offers
`specflo auto` for it."""

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from specflo import auto, cli, config, continuation, projects
from specflo.cli import app
from specflo.errors import SpecfloError
from specflo.service.local import LocalProjectService

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
    """A git repository with one commit and an active research project, as the cwd."""
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Tester")
    (tmp_path / "app.txt").write_text("hello\n")
    _ok(["init"])
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    _ok(["new", "Thing", "--level", "research"])
    return tmp_path


def _files(repo):
    """Every file outside .git, .specflo included, with its bytes and its
    modification time: byte-identical and untouched, or the test fails."""
    return {
        path.relative_to(repo).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }


def _refs(repo):
    return git(repo, "for-each-ref", "--format=%(refname) %(objectname)")


# --- the refusal ---------------------------------------------------------------


def test_research_is_an_attended_level_beside_harden():
    assert auto.ATTENDED_LEVELS == (projects.HARDEN_LEVEL, projects.RESEARCH_LEVEL)


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
def test_auto_refuses_a_research_project_naming_its_level_and_changes_no_file(repo, args):
    before = _files(repo)

    result = runner.invoke(app, args)

    assert result.exit_code != 0
    assert isinstance(result.exception, SpecfloError)
    message = str(result.exception)
    assert "'thing'" in message
    assert "research level" in message and "attended work" in message
    assert "at the keyboard" in message
    assert "specflo-research-project" in message
    assert result.output == ""
    assert _files(repo) == before
    assert not (repo / "docs" / "projects" / "thing" / "auto-run.json").exists()


def test_the_refusal_reaches_the_user_as_an_error_with_exit_1(repo, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_notify_stale_skills", lambda: None)
    monkeypatch.setattr(sys, "argv", ["specflo", "auto", "--json"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    shown = capsys.readouterr()
    assert exc.value.code == 1
    assert shown.out == ""
    assert shown.err.startswith("error: ") and "research level" in shown.err


def test_the_ladder_refuses_a_research_project_as_it_refuses_any_level_but_quick(repo):
    files, refs = _files(repo), _refs(repo)
    head = git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    result = runner.invoke(app, ["auto", "--ladder", "--json"])

    assert result.exit_code != 0
    assert "A ladder starts at quick level" in result.output
    assert "'research'" in result.output
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == head
    assert _refs(repo) == refs
    assert _files(repo) == files


def test_the_auto_payload_functions_refuse_a_research_project_too(repo):
    before = _files(repo)

    for emit in (auto.auto_text, auto.auto_pass_result, auto.auto_pass):
        with pytest.raises(SpecfloError, match="research level"):
            emit(repo)

    assert _files(repo) == before


def test_after_the_refusals_no_auto_run_is_under_way(repo):
    for args in (["auto", "--on"], ["auto"], ["auto", "--ladder"]):
        assert runner.invoke(app, args).exit_code != 0

    info = json.loads(_ok(["status", "--json"]).output)

    assert info["auto_run"] == {"under_way": False}


def test_the_harden_refusal_still_names_harden(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ok(["init"])
    _ok(["new", "Thing", "--level", "harden"])

    result = runner.invoke(app, ["auto"])

    assert result.exit_code != 0
    assert "harden level" in str(result.exception)
    assert "research" not in str(result.exception)


# --- the continuation never offers auto on a research project -----------------
#
# A research project has no plan, so no task completes on it, and its one
# phase is first and last, so nothing reopens it: the two seams that print the
# offer never fire for it today. The suppression is asserted on the helpers
# the seams share, so a seam a later change gives the level takes the same path.


def _seam(root, slug):
    cfg = config.load_config(root)
    svc = LocalProjectService(root, cfg)
    return (
        cli._seam_continuation(svc, slug, root, cfg)["continuation"],
        cli._offers_auto(svc, slug),
    )


def test_the_seam_continuation_on_a_research_project_resumes_with_the_checkpoint_alone(repo):
    text, offers = _seam(repo, "thing")

    assert offers is False
    assert text is not None
    assert "Current phase: research." in text
    assert "specflo-research-project" in text
    assert continuation.CLEAR_POINT_MARKER in text
    assert "resume with `specflo checkpoint`." in text
    assert "specflo auto" not in text


def test_the_seam_continuation_on_a_full_project_still_offers_auto(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ok(["init"])
    _ok(["new", "Thing"])

    text, offers = _seam(tmp_path, "thing")

    assert offers is True
    assert "(or `specflo auto` in an auto run)" in text


def test_the_seam_fallback_on_a_research_project_does_not_offer_auto(repo):
    cfg = config.load_config(repo)
    svc = LocalProjectService(repo, cfg)

    line = continuation.clear_point_only(offer_auto=cli._offers_auto(svc, "thing"))

    assert line == f"{continuation.CLEAR_POINT_MARKER} - resume with `specflo checkpoint`."
