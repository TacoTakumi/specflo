"""The notice-only stale-skills startup check wired at specflo's main().

check_stale runs at the CLI entry point over the ZERO-ARG default union
(default_source('specflo')), so it measures staleness against what
`specflo skills` actually installs (Root B repo skills), not a bare
BundledPackageDataSource (Root A only, empty for specflo). It prints exactly one
stderr line when an installed skill is update-available, never changes the exit
code, and can never break the command it runs inside.

Staleness is seeded against throwaway fixture roots (D-12): a modified copy of a
real skill is installed with a specflo stamp, so it reads as update-available
against specflo's real source -- the real ~/.claude is never touched.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import specflo
from agentsquire import CLAUDE_CODE, install
from agentsquire.sources import DirectorySource

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECFLO_BIN = str(Path(sys.executable).parent / "specflo")


def _seed_stale_plan(root):
    """Install a modified 'plan' skill (specflo-stamped) into a fixture home so
    it reads as update-available against specflo's real repo-root source."""
    home = root / "home"
    project = root / "project"
    bundle = root / "bundle"
    (home / ".claude").mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    bundle.mkdir()
    shutil.copytree(REPO_ROOT / "skills" / "plan", bundle / "plan")
    skill_md = bundle / "plan" / "SKILL.md"
    skill_md.write_text(skill_md.read_text() + "\n<!-- older bundled version -->\n")
    install(
        DirectorySource(bundle),
        CLAUDE_CODE,
        scope="user",
        home=home,
        project=project,
        source_package="specflo",
        source_version=specflo.__version__,
    )
    return home, project


def _run_specflo(home, project, *args):
    env = {
        **os.environ,
        "AGENTSQUIRE_HOME": str(home),
        "AGENTSQUIRE_PROJECT": str(project),
    }
    env.pop("CI", None)  # CI/AGENTSQUIRE_NO_UPDATE_CHECK suppress the notice
    env.pop("AGENTSQUIRE_NO_UPDATE_CHECK", None)
    return subprocess.run(
        [SPECFLO_BIN, *args], capture_output=True, text=True, env=env
    )


def test_stale_install_prints_one_line_notice_naming_the_update_command(tmp_path):
    home, project = _seed_stale_plan(tmp_path)
    result = _run_specflo(home, project, "--version")
    assert result.returncode == 0
    assert "plan" in result.stderr
    assert "specflo skills update" in result.stderr
    assert result.stderr.strip().count("\n") == 0  # exactly one line
    assert result.stdout.strip() == f"specflo {specflo.__version__}"  # command ran


def test_notice_leaves_the_exit_code_unchanged(tmp_path):
    home, project = _seed_stale_plan(tmp_path)
    stale = _run_specflo(home, project, "--version")
    clean_home = tmp_path / "clean"
    (clean_home / ".claude").mkdir(parents=True)
    clean = _run_specflo(clean_home, project, "--version")
    assert clean.stderr.strip() == ""  # nothing installed -> no notice
    assert stale.returncode == clean.returncode == 0


def test_no_notice_when_nothing_is_installed(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".claude").mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    result = _run_specflo(home, project, "--version")
    assert result.returncode == 0
    assert result.stderr.strip() == ""


def test_symlinked_dev_install_prints_no_notice(tmp_path):
    # A symlink at the target is locally-modified, not update-available -- the
    # real ~/.claude/skills -> repo skills/ dev setup must never fire a notice.
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".claude" / "skills").mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    (home / ".claude" / "skills" / "plan").symlink_to(REPO_ROOT / "skills" / "plan")
    result = _run_specflo(home, project, "--version")
    assert result.returncode == 0
    assert result.stderr.strip() == ""


def test_startup_check_failure_does_not_break_the_command(monkeypatch, capsys):
    # Belt-and-suspenders: even if check_stale raised, main() completes the
    # command with its normal output and exit code.
    import specflo.cli as cli

    def boom(*args, **kwargs):
        raise RuntimeError("staleness check exploded")

    monkeypatch.setattr(cli, "check_stale", boom)
    monkeypatch.setattr(sys, "argv", ["specflo", "--version"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code in (0, None)
    assert capsys.readouterr().out.strip() == f"specflo {specflo.__version__}"


def test_check_is_wired_over_the_default_union_not_bundled_package_data():
    # Structural guard for the acceptance's exact wiring.
    import inspect

    import specflo.cli as cli

    source = inspect.getsource(cli._notify_stale_skills)
    assert "default_source(" in source  # the union call
    assert "BundledPackageDataSource(" not in source  # never the bare Root A call
    assert 'prog_name="specflo"' in source
    assert 'update_command="specflo skills update"' in source
