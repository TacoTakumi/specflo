"""`specflo doctor` checks the command is on PATH and each harness has the skills."""

import json
import shutil

import pytest
from typer.testing import CliRunner

from agentsquire.harnesses import CLAUDE_CODE
from agentsquire.sources import DirectorySource
from agentsquire.verbs import install

from specflo import doctor
from specflo.cli import app


def _skill(root, name, body="Does a thing."):
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Use when testing {name}.\n---\n\n{body}\n"
    )
    return path


@pytest.fixture
def src(tmp_path):
    root = tmp_path / "bundled"
    _skill(root, "specflo-alpha")
    _skill(root, "specflo-beta")
    return root


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)  # claude-code is the one detected harness
    return home


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    return project


def _run(home, project, src, which="/usr/local/bin/specflo"):
    return doctor.run_checks(
        home=home, project=project, source=DirectorySource(src), which=lambda _: which
    )


def _install(home, project, src):
    install(DirectorySource(src), CLAUDE_CODE, scope="user", home=home, project=project,
            source_package="specflo", source_version="0.0.0")


def _by_subject(checks):
    return {c.subject: c for c in checks}


def _failures(checks):
    return [c for c in checks if c.status == "fail"]


def test_a_full_install_passes(home, project, src):
    _install(home, project, src)
    checks = _run(home, project, src)
    assert _failures(checks) == []
    got = _by_subject(checks)
    assert got["specflo on PATH"].detail == "/usr/local/bin/specflo"
    assert got["claude-code (user)"].detail == "2 of 2 skills installed"


def test_a_full_set_of_links_to_matching_content_passes(home, project, src):
    skills = home / ".claude" / "skills"
    skills.mkdir()
    for name in ("specflo-alpha", "specflo-beta"):
        (skills / name).symlink_to(src / name)
    checks = _run(home, project, src)
    assert _failures(checks) == []
    assert _by_subject(checks)["claude-code (user)"].detail == (
        "2 of 2 skills installed (2 as links)"
    )


def test_the_command_missing_from_path_is_a_problem(home, project, src):
    _install(home, project, src)
    [failure] = _failures(_run(home, project, src, which=None))
    assert failure.subject == "specflo on PATH"
    assert "uv tool install specflo" in failure.fix


def test_a_partial_install_names_the_missing_skill(home, project, src):
    _install(home, project, src)
    shutil.rmtree(home / ".claude" / "skills" / "specflo-beta")
    failures = _failures(_run(home, project, src))
    by = _by_subject(failures)
    assert by["claude-code (user)"].detail == "missing: specflo-beta"
    assert by["claude-code (user)"].fix == "`specflo skills install --harness claude-code:user`"
    assert "No agent harness has every specflo skill" in by["skills"].detail


def test_a_stale_install_asks_for_an_update(home, project, src):
    _install(home, project, src)
    _skill(src, "specflo-alpha", body="Does a newer thing.")
    [failure, *_] = _failures(_run(home, project, src))
    assert failure.detail == "stale: specflo-alpha"
    assert failure.fix == "`specflo skills update --harness claude-code:user`"


def test_a_broken_or_mismatched_link_is_a_problem(home, project, src, tmp_path):
    skills = home / ".claude" / "skills"
    skills.mkdir()
    (skills / "specflo-alpha").symlink_to(tmp_path / "gone")
    (skills / "specflo-beta").symlink_to(_skill(tmp_path / "other", "specflo-beta", "Old."))
    [failure, *_] = _failures(_run(home, project, src))
    assert failure.detail == "broken or mismatched link: specflo-alpha, specflo-beta"
    assert failure.fix.startswith("remove the link")
    assert "`specflo skills install --harness claude-code:user`" in failure.fix


def test_a_locally_modified_copy_is_a_problem(home, project, src):
    _install(home, project, src)
    (home / ".claude" / "skills" / "specflo-alpha" / "SKILL.md").write_text("edited\n")
    [failure, *_] = _failures(_run(home, project, src))
    assert failure.detail == "locally modified: specflo-alpha"
    assert failure.fix == "`specflo skills update --force --harness claude-code:user`"


def test_no_installed_harness_is_a_problem(home, project, src):
    checks = _run(home, project, src)
    got = _by_subject(checks)
    assert got["claude-code"].status == "skip"
    assert got["claude-code"].detail == "detected, no specflo skills"
    assert got["skills"].status == "fail"
    assert got["skills"].fix == "`specflo skills install`"


def test_the_command_prints_each_check_and_exits_1_on_a_problem(
    home, project, monkeypatch
):
    monkeypatch.setenv("AGENTSQUIRE_HOME", str(home))
    monkeypatch.setenv("AGENTSQUIRE_PROJECT", str(project))
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/local/bin/specflo")
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "ok    specflo on PATH: /usr/local/bin/specflo" in result.output
    assert "FAIL  skills: No agent harness has every specflo skill." in result.output
    assert "Fix: `specflo skills install`" in result.output
    assert result.output.rstrip().endswith("1 problem found.")

    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert {"status", "subject", "detail", "fix"} <= set(data["checks"][0])


# --- the release line -----------------------------------------------------------


def _release(status, installed="1.2.0", newest="1.2.0", command=None, error=None):
    from agentsquire import ReleaseCheck, ReleaseStatus

    calls = []

    def check():
        calls.append(1)
        return ReleaseCheck(
            distribution="specflo", installed=installed, newest=newest,
            status=ReleaseStatus(status), upgrade_command=command, error=error,
        )

    check.calls = calls
    return check


def _release_line(check, environ=None):
    return doctor.release_check(check, environ={} if environ is None else environ)


def test_the_release_line_is_ok_when_specflo_is_the_newest():
    line = _release_line(_release("up-to-date"))
    assert (line.status, line.subject) == ("ok", "release")
    assert "1.2.0 is the newest release" in line.detail


def test_the_release_line_is_ok_when_specflo_is_ahead_of_pypi():
    line = _release_line(_release("ahead", installed="1.3.0"))
    assert line.status == "ok"
    assert "1.3.0" in line.detail and "1.2.0" in line.detail


def test_the_release_line_names_a_newer_release_and_is_not_a_failure():
    line = _release_line(
        _release("update-available", newest="1.4.0", command="uv tool upgrade specflo")
    )
    assert line.status == "note"
    assert "1.2.0 -> 1.4.0" in line.detail
    assert line.fix == "`uv tool upgrade specflo`"
    report = doctor.render([line])
    assert report.splitlines()[0].startswith("note  release: ")
    assert report.endswith("All checks passed.")


def test_the_release_line_is_skipped_with_the_reason_when_the_check_fails():
    line = _release_line(
        _release("check-failed", newest=None, error="no route to host")
    )
    assert line.status == "skip"
    assert "no route to host" in line.detail


@pytest.mark.parametrize("variable", ["CI", "AGENTSQUIRE_NO_UPDATE_CHECK"])
def test_the_release_line_makes_no_request_when_a_variable_switches_it_off(variable):
    check = _release("update-available", newest="1.4.0")
    line = _release_line(check, environ={variable: "1"})
    assert line.status == "skip"
    assert variable in line.detail
    assert check.calls == []


def test_a_release_check_that_raises_is_a_skipped_line():
    def check():
        raise RuntimeError("boom")

    assert _release_line(check).status == "skip"


def test_doctor_ends_with_the_release_line_and_keeps_its_exit_code(src, home, project):
    # The line comes last, also when the setup checks stop early, and a newer
    # release does not turn a passing doctor into a failing one.
    _install(home, project, src)
    check = _release("update-available", newest="1.4.0")
    checks = doctor.run_checks(
        home=home, project=project, source=DirectorySource(src),
        which=lambda name: "/usr/bin/specflo", release=check, environ={},
    )
    assert checks[-1].subject == "release"
    assert not any(c.status == "fail" for c in checks)

    empty = project / "nothing"
    empty.mkdir()
    early = doctor.run_checks(
        home=empty, project=empty, source=DirectorySource(src),
        which=lambda name: None, release=check, environ={},
    )
    assert early[-1].subject == "release"


def test_the_doctor_command_carries_the_release_line_in_json(monkeypatch, tmp_path):
    # The suite keeps PyPI out of reach, so the command reports a skipped line.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("AGENTSQUIRE_NO_UPDATE_CHECK", raising=False)
    data = json.loads(CliRunner().invoke(app, ["doctor", "--json"]).output)
    release = [c for c in data["checks"] if c["subject"] == "release"]
    assert len(release) == 1
    assert release[0]["status"] == "skip"
