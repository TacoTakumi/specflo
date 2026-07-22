"""The bundled pi extension installs from package data, offline, with a stamp.

The extension source lives at ``src/specflo/extension/`` and rides into the
wheel as ordinary package data (T-07 / REQ-23). ``specflo extension install``
copies that tree into pi's extension directory and writes a provenance stamp
whose ``source_version`` is the running ``specflo --version`` -- the same
installer shape agentsquire uses for skills (harness detection, scopes, version
stamp, stale notice), minus the SKILL.md frontmatter that has no analogue here.

Two negative guarantees are load-bearing and get their own tests: the install
touches no network and spawns no process (REQ-23's no-npm-fetch clause), and no
publish path exists anywhere in the repo (REQ-24).
"""

import json
import re
import socket
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import __version__, extension_install
from specflo.cli import app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def pi_home(tmp_path):
    """A throwaway home with pi's marker dir, so the harness is detected."""
    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    return home


@pytest.fixture
def pi_project(tmp_path):
    """A throwaway project root carrying pi's project marker."""
    project = tmp_path / "project"
    (project / ".pi").mkdir(parents=True)
    return project


# --- the shipped source tree ------------------------------------------------


def test_extension_source_ships_inside_the_specflo_package():
    source = extension_install.extension_source()
    assert source.is_dir()
    assert source.parent == Path(extension_install.__file__).resolve().parent
    assert (source / "package.json").is_file()
    assert (source / "src" / "index.ts").is_file()


def test_package_json_declares_the_pi_extension_entry():
    manifest = json.loads(
        (extension_install.extension_source() / "package.json").read_text()
    )
    entries = manifest["pi"]["extensions"]
    assert entries, "package.json declares no pi.extensions entry"
    for entry in entries:
        assert (extension_install.extension_source() / entry).is_file()


def test_package_json_declares_no_publish_configuration():
    # REQ-24: no npm publication path in v1. `publishConfig` is npm's publish
    # switch; `private: true` is npm's hard refusal to publish at all.
    manifest = json.loads(
        (extension_install.extension_source() / "package.json").read_text()
    )
    assert "publishConfig" not in manifest
    assert manifest.get("private") is True


def test_no_release_workflow_targets_the_npm_registry():
    # REQ-24's second clause. Scans every CI workflow and the release config for
    # an npm publish target; today there are no workflows at all, so this test
    # is the tripwire that fails if one lands carrying a publish step.
    candidates = list((REPO_ROOT / ".github").rglob("*.yml"))
    candidates += list((REPO_ROOT / ".github").rglob("*.yaml"))
    candidates += [REPO_ROOT / ".releaserc"]
    pattern = re.compile(r"npm\s+publish|registry\.npmjs\.org|NPM_TOKEN")
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in candidates
        if p.is_file() and pattern.search(p.read_text())
    ]
    assert not offenders, f"npm publish path found in: {offenders}"


# --- install ----------------------------------------------------------------


def test_install_places_the_extension_under_pis_user_extension_dir(pi_home, pi_project):
    installed = extension_install.install_extension(home=pi_home, project=pi_project)

    target = pi_home / ".pi" / "agent" / "extensions" / extension_install.EXTENSION_NAME
    assert installed.path == target
    assert (target / "package.json").is_file()
    assert (target / "src" / "index.ts").is_file()


def test_installed_tree_is_byte_identical_to_the_source(pi_home, pi_project):
    installed = extension_install.install_extension(home=pi_home, project=pi_project)
    source = extension_install.extension_source()

    for file in sorted(p for p in source.rglob("*") if p.is_file()):
        copied = installed.path / file.relative_to(source)
        assert copied.read_bytes() == file.read_bytes()


def test_install_records_a_version_stamp_equal_to_specflo_version(pi_home, pi_project):
    installed = extension_install.install_extension(home=pi_home, project=pi_project)

    stamp = json.loads(
        (installed.path / extension_install.STAMP_FILENAME).read_text()
    )
    assert stamp["source_version"] == __version__
    assert stamp["installer"] == "specflo"
    assert stamp["source_package"] == "specflo"
    assert stamp["content_hash"] == extension_install.extension_content_hash(
        extension_install.extension_source()
    )


def test_stamp_version_matches_what_specflo_version_prints(pi_home, pi_project):
    installed = extension_install.install_extension(home=pi_home, project=pi_project)
    stamp = json.loads((installed.path / extension_install.STAMP_FILENAME).read_text())

    printed = runner.invoke(app, ["--version"]).stdout.strip()
    assert printed == f"specflo {stamp['source_version']}"


def test_install_into_project_scope_uses_pis_project_extension_dir(pi_home, pi_project):
    installed = extension_install.install_extension(
        scope="project", home=pi_home, project=pi_project
    )
    assert installed.path == pi_project / ".pi" / "extensions" / "specflo"
    assert (installed.path / "package.json").is_file()


def test_install_is_idempotent_and_reports_state(pi_home, pi_project):
    first = extension_install.install_extension(home=pi_home, project=pi_project)
    assert first.state == "installed"

    second = extension_install.install_extension(home=pi_home, project=pi_project)
    assert second.state == "current"
    assert second.path == first.path


def test_install_over_a_stale_copy_reports_updated_and_replaces_it(pi_home, pi_project):
    installed = extension_install.install_extension(home=pi_home, project=pi_project)
    (installed.path / "src" / "index.ts").write_text("// drifted\n")
    stray = installed.path / "src" / "leftover.ts"
    stray.write_text("// removed on update\n")

    again = extension_install.install_extension(home=pi_home, project=pi_project)

    assert again.state == "updated"
    assert not stray.exists()
    assert (again.path / "src" / "index.ts").read_bytes() == (
        extension_install.extension_source() / "src" / "index.ts"
    ).read_bytes()


def test_install_replaces_a_symlinked_target_without_following_it(
    pi_home, pi_project, tmp_path
):
    # Symlink-safe removal: unlink the link, never rmtree through it.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    keep = elsewhere / "keep.txt"
    keep.write_text("untouched\n")
    target = pi_home / ".pi" / "agent" / "extensions" / "specflo"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(elsewhere)

    extension_install.install_extension(home=pi_home, project=pi_project)

    assert keep.read_text() == "untouched\n"
    assert not target.is_symlink()
    assert (target / "package.json").is_file()


def test_install_errors_when_pi_is_not_detected(tmp_path):
    bare_home = tmp_path / "bare-home"
    bare_project = tmp_path / "bare-project"
    bare_home.mkdir()
    bare_project.mkdir()

    with pytest.raises(extension_install.ExtensionInstallError) as exc:
        extension_install.install_extension(home=bare_home, project=bare_project)
    assert "pi" in str(exc.value)


def test_install_rejects_an_unknown_scope(pi_home, pi_project):
    with pytest.raises(extension_install.ExtensionInstallError) as exc:
        extension_install.install_extension(
            scope="global", home=pi_home, project=pi_project
        )
    assert "global" in str(exc.value)


# --- offline guarantee ------------------------------------------------------


def test_install_opens_no_socket_and_spawns_no_process(
    pi_home, pi_project, monkeypatch
):
    # REQ-23: the install is a package-data copy. Any socket or child process
    # would mean a fetch -- from the npm registry or anywhere else.
    def no_network(*args, **kwargs):
        raise AssertionError("install attempted network access")

    def no_subprocess(*args, **kwargs):
        raise AssertionError("install spawned a process")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)

    installed = extension_install.install_extension(home=pi_home, project=pi_project)
    assert (installed.path / "package.json").is_file()


def test_install_module_references_no_package_manager():
    # A source-level tripwire on the same guarantee: nothing in the install path
    # names npm/npx/yarn/pnpm or a registry URL.
    source = Path(extension_install.__file__).read_text().lower()
    for token in ("npm ", "npx", "yarn", "pnpm", "npmjs.org"):
        assert token not in source, f"install module references {token!r}"


# --- CLI --------------------------------------------------------------------


def test_cli_extension_install_exits_zero_and_names_the_target(
    pi_home, pi_project, monkeypatch
):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: pi_home))
    monkeypatch.chdir(pi_project)

    result = runner.invoke(app, ["extension", "install"])

    assert result.exit_code == 0, result.stdout
    target = pi_home / ".pi" / "agent" / "extensions" / "specflo"
    assert (target / "src" / "index.ts").is_file()
    assert __version__ in result.stdout


def test_cli_extension_install_project_scope(pi_home, pi_project, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: pi_home))
    monkeypatch.chdir(pi_project)

    result = runner.invoke(app, ["extension", "install", "--scope", "project"])

    assert result.exit_code == 0, result.stdout
    assert (pi_project / ".pi" / "extensions" / "specflo" / "package.json").is_file()


def test_cli_extension_install_reports_a_missing_harness_without_a_traceback(
    tmp_path, monkeypatch
):
    bare_home = tmp_path / "bare-home"
    bare_home.mkdir()
    bare_project = tmp_path / "bare-project"
    bare_project.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: bare_home))
    monkeypatch.chdir(bare_project)

    result = runner.invoke(app, ["extension", "install"])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "pi is not detected" in result.stderr  # a message, not a traceback
