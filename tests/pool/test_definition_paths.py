"""A definition's extra read-only paths, and the ones pool validation refuses.

A definition may list paths to bind back, read-only, into the sandbox of each
member it runs as: a tool installed under the home, or data a role reads. A
leading ``~`` stands for the home. The sandbox hides the home, the operator's
directories under it, the runtime directory, the daemon's root and every
checkout's token directories, and a bind brings back whatever it covers. So a
listed path is refused when it is at or above any hidden path, when it lies in
a hidden path other than the home, when it lies in the fresh /tmp or /run and
not in the home, when it lies in the sandbox's own /proc or /dev, when it is a
link whose real path is any of those, and when
it does not exist. Each refusal names the path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo.cli import app as cli
from specflo.pool import cli_admin, definitions, launch
from specflo.pool import config as pool_config

runner = CliRunner()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A home with the operator's hidden directories in it, a daemon root in
    the home as an operator keeps one, and its pool directory."""
    home = tmp_path / "home"
    for name in (".pi/agent/sessions", ".agents", ".specflo", "tools/bin", "proj/.specflo/leases"):
        (home / name).mkdir(parents=True)
    (home / "proj" / ".specflo" / "config.yaml").write_text("projects_dir: docs\n")
    (home / "tools" / "data.txt").write_text("data\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    (runtime / "bus").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    root = home / "specflo-daemon"
    done = runner.invoke(cli, ["serve", "--root", str(root), "pool", "init"])
    assert done.exit_code == 0, done.output
    return {"home": home, "runtime": runtime, "root": root, "tmp": tmp_path}


def listing(rig, *paths: str) -> Path:
    """A definition 'lister' in the rig's pool directory that lists *paths*."""
    folder = cli_admin.pool_dir(rig["root"]) / pool_config.DEFINITIONS_DIR
    path = folder / "lister.md"
    path.write_text(definitions.serialise_definition(definitions.AgentDefinition(
        name="lister", role="Reads what it is given", prompt="You read.", paths=paths,
    )), encoding="utf-8")
    return path


def faults(rig) -> list[pool_config.ConfigError]:
    _, errors = pool_config.load_pool_config(cli_admin.pool_dir(rig["root"]))
    return [e for e in errors if e.entry == "definition 'lister'"]


def test_a_definition_carries_its_listed_paths_as_written(tmp_path):
    path = tmp_path / "lister.md"
    path.write_text(
        "---\nrole: Reads\npaths: [~/tools/bin, /opt/data]\n---\n\nYou read.\n", encoding="utf-8"
    )

    loaded = definitions.load_definition(path)

    assert loaded.paths == ("~/tools/bin", "/opt/data")
    assert definitions.parse_definition(definitions.serialise_definition(loaded), path) == loaded


def test_paths_under_the_home_that_hide_nothing_pass(rig):
    listing(rig, "~/tools/bin", str(rig["home"] / "tools" / "data.txt"))

    config, errors = pool_config.load_pool_config(cli_admin.pool_dir(rig["root"]))

    assert errors == []
    (lister,) = [d for d in config.definitions if d.name == "lister"]
    assert lister.paths == ("~/tools/bin", str(rig["home"] / "tools" / "data.txt"))


REFUSED = {
    "missing": "~/not-there",
    "the home": "~",
    "above the home": "{tmp}",
    "the filesystem root": "/",
    "a parent of the daemon root": "{home}",
    "the daemon root": "~/specflo-daemon",
    "inside the daemon root": "~/specflo-daemon/pool",
    "inside the pi directory": "~/.pi/agent/sessions",
    "the agents directory": "~/.agents",
    "the agent state directory": "~/.specflo",
    "inside the runtime directory": "{runtime}/bus",
    "a checkout's token directory": "~/proj/.specflo/leases",
    "a checkout that holds token directories": "~/proj",
    "a relative path": "tools/bin",
    "the fresh /run": "/run",
    "in the fresh /tmp, outside the home": "{tmp}/outside",
    "the sandbox's own /proc": "/proc",
    "the sandbox's own /dev": "/dev",
    "the host's shared memory": "/dev/shm",
}


@pytest.mark.parametrize("case", REFUSED)
def test_a_path_that_would_bring_back_what_is_hidden_is_refused_naming_it(rig, case):
    listed = REFUSED[case].format(tmp=rig["tmp"], home=rig["home"], runtime=rig["runtime"])
    (rig["tmp"] / "outside").mkdir(exist_ok=True)
    listing(rig, "~/tools/bin", listed)

    found = faults(rig)

    assert [(fault.field) for fault in found] == ["paths"], found
    assert listed in str(found[0])


@pytest.mark.parametrize(
    "target", ["~/.pi/agent", "~/specflo-daemon", "~/proj/.specflo/leases", "/run", "/dev/shm"]
)
def test_a_link_whose_real_path_is_hidden_is_refused_naming_it(rig, target):
    link = rig["home"] / "tools" / "innocent"
    link.symlink_to(Path(target.replace("~", str(rig["home"]))))
    listing(rig, "~/tools/innocent")

    found = faults(rig)

    assert [fault.field for fault in found] == ["paths"], found
    assert "~/tools/innocent" in str(found[0])


def test_a_refused_definition_does_not_stand(rig):
    listing(rig, "~")

    config, _ = pool_config.load_pool_config(cli_admin.pool_dir(rig["root"]))

    assert "lister" not in [d.name for d in config.definitions]


def test_pool_validate_reports_the_path(rig):
    listing(rig, "~/.pi/agent/sessions")

    done = runner.invoke(cli, ["serve", "--root", str(rig["root"]), "pool", "validate"])

    assert done.exit_code == 1
    assert "~/.pi/agent/sessions" in done.output


def test_loading_the_pool_records_where_each_listed_path_leads(rig):
    (rig["home"] / "link").symlink_to(rig["home"] / "tools")
    listing(rig, "~/tools/bin", "~/link")

    config, _ = pool_config.load_pool_config(cli_admin.pool_dir(rig["root"]))

    (lister,) = [d for d in config.definitions if d.name == "lister"]
    tools = str((rig["home"] / "tools").resolve())
    assert lister.resolved == (
        (str(rig["home"] / "tools" / "bin"), tools + "/bin"),
        (str(rig["home"] / "link"), str(rig["home"] / "tools"), tools),
    )
    assert lister.resolved == tuple(
        launch.listed_resolution(path, {"HOME": str(rig["home"])}) for path in lister.paths
    )
