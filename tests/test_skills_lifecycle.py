"""The specflo skills install/status/update/uninstall lifecycle and provenance.

Driven through the mounted CLI against throwaway AGENTSQUIRE_HOME /
AGENTSQUIRE_PROJECT fixture roots (D-12) -- never the real ~/.claude. Verbs run
to completion (no --help), so click's CliRunner drives the mounted group
directly, as agentsquire's own consumer tests do.

Drift/restore note: mutating an *installed* copy makes it "locally-modified",
which agentsquire deliberately will NOT clobber on a plain `update` (it protects
user edits); restore is via `update --force`. That is the path proven here. The
"update-available" class (the bundled source moved on) is the other path where a
plain `update` restores, but exercising it would require mutating the real
repo-root skills/ source, so it is left to agentsquire's own suite.
"""

from click.testing import CliRunner

from agentsquire import read_stamp
from specflo import __version__
from specflo.cli import build_cli

SKILL_NAMES = [
    "specflo-brainstorm",
    "specflo-execute",
    "specflo-plan",
    "specflo-research",
    "specflo-shelve",
    "specflo-spec",
]


def _run(*args):
    return CliRunner().invoke(build_cli(), list(args), catch_exceptions=False)


def _user_skill(home, name):
    return home / ".claude" / "skills" / name / "SKILL.md"


def test_install_places_all_six_with_specflo_provenance(fixture_roots):
    home, _ = fixture_roots
    result = _run("skills", "install")
    assert result.exit_code == 0, result.output
    for name in SKILL_NAMES:
        skill_md = _user_skill(home, name)
        assert skill_md.is_file(), f"{name} not installed"
        stamp = read_stamp(skill_md.read_text())
        assert stamp is not None, f"{name} carries no provenance stamp"
        assert stamp["source_package"] == "specflo"
        assert stamp["source_version"] == __version__


def test_status_reports_all_six_installed_and_current(fixture_roots):
    _run("skills", "install")
    result = _run("skills", "status")
    assert result.exit_code == 0
    for name in SKILL_NAMES:
        assert f"up-to-date {name}" in result.output


def test_mutated_install_is_detected_then_force_update_restores(fixture_roots):
    home, _ = fixture_roots
    assert _run("skills", "install").exit_code == 0

    plan_md = _user_skill(home, "specflo-plan")
    plan_md.write_text(plan_md.read_text() + "\n<!-- tampered -->\n")

    # Drift is detected as locally-modified.
    assert "locally-modified specflo-plan" in _run("skills", "status").output

    # A plain update deliberately does NOT clobber the user's edit.
    assert _run("skills", "update").exit_code == 0
    assert "locally-modified specflo-plan" in _run("skills", "status").output
    assert "<!-- tampered -->" in plan_md.read_text()

    # --force restores it to the bundled copy.
    assert _run("skills", "update", "--force").exit_code == 0
    assert "up-to-date specflo-plan" in _run("skills", "status").output
    assert "<!-- tampered -->" not in plan_md.read_text()


def test_default_scope_installs_into_the_user_root(fixture_roots):
    home, project = fixture_roots
    result = _run("skills", "install")
    assert result.exit_code == 0, result.output
    assert _user_skill(home, "specflo-plan").is_file()
    assert not (project / ".claude" / "skills").exists()


def test_scope_project_flag_routes_to_the_project_root(fixture_roots):
    home, project = fixture_roots
    result = _run("skills", "install", "--scope", "project")
    assert result.exit_code == 0, result.output
    assert (project / ".claude" / "skills" / "specflo-plan" / "SKILL.md").is_file()
    assert not (home / ".claude" / "skills").exists()


def test_uninstall_removes_all_six(fixture_roots):
    home, _ = fixture_roots
    _run("skills", "install")
    result = _run("skills", "uninstall")
    assert result.exit_code == 0
    for name in SKILL_NAMES:
        assert not _user_skill(home, name).parent.exists(), f"{name} not removed"
