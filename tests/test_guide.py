import json
import re
from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from specflo import config, guide, projects
from specflo.cli import app

runner = CliRunner()


@pytest.fixture
def cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- build_guide: the three repo states ---------------------------------


def test_build_guide_uninitialized():
    data = guide.build_guide(None, None)
    assert data["initialized"] is False
    assert data["next_action"] == "init"
    assert data["pipeline"] == ["brainstorm", "spec", "plan", "execute"]
    assert data["commands"], "command table should not be empty"
    assert "phase" not in data


def test_build_guide_initialized_no_active(tmp_path):
    cfg = config.init_config(tmp_path)
    data = guide.build_guide(tmp_path, cfg)
    assert data["initialized"] is True
    assert data["active_project"] is None
    assert data["next_action"] == "new"
    assert "phase" not in data


def test_build_guide_active_project(tmp_path):
    cfg = config.init_config(tmp_path)
    project = projects.create_project(tmp_path, cfg, "My Thing")
    cfg.active_project = project.slug
    data = guide.build_guide(tmp_path, cfg)
    assert data["active_project"] == "my-thing"
    assert data["phase"] == "brainstorm"
    assert data["next_action"] == "brainstorm"
    assert data["next_step"], "active project should carry a next-step hint"


def test_build_guide_active_project_that_wont_load(tmp_path):
    # active_project set in config but the project dir is missing -> stays
    # useful, falls back to the "new" guidance rather than blowing up.
    cfg = config.init_config(tmp_path)
    cfg.active_project = "ghost"
    data = guide.build_guide(tmp_path, cfg)
    assert data["initialized"] is True
    assert data["next_action"] == "new"


# --- coverage guard: the table must list every CLI command --------------


def _leaf_paths(command, prefix=()):
    commands = getattr(command, "commands", None)
    if not commands:
        return [prefix]
    paths = []
    for name, sub in commands.items():
        paths.extend(_leaf_paths(sub, prefix + (name,)))
    return paths


def test_guide_table_covers_every_cli_command():
    cli = get_command(app)
    covered = {entry["name"] for entry in guide.COMMANDS}
    for path in _leaf_paths(cli):
        name = " ".join(path)
        assert name in covered, f"guide.COMMANDS is missing {name!r}"


# --- the CLI command: runs cold, in every state -------------------------


def test_guide_runs_cold_in_uninitialized_repo(cwd):
    result = runner.invoke(app, ["guide"])
    assert result.exit_code == 0
    out = result.output.lower()
    assert "init" in out  # tells you how to start
    for phase in ("brainstorm", "spec", "plan", "execute"):
        assert phase in out  # the pipeline is shown


def test_guide_json_uninitialized(cwd):
    data = json.loads(runner.invoke(app, ["guide", "--json"]).output)
    assert data["initialized"] is False
    assert data["next_action"] == "init"
    assert data["pipeline"][0] == "brainstorm"
    assert any(c["name"] == "advance" for c in data["commands"])


def test_guide_offers_paste_ready_memory_snippet(cwd):
    # The paste-into-CLAUDE.md snippet shows in the text output, points at the
    # live commands rather than embedding them, and carries no version string
    # (it must never need re-syncing on a specflo upgrade).
    result = runner.invoke(app, ["guide"])
    assert result.exit_code == 0
    assert guide.MEMORY_SNIPPET in result.output
    assert "CLAUDE.md" in result.output
    assert "specflo guide" in guide.MEMORY_SNIPPET
    assert "v0." not in guide.MEMORY_SNIPPET  # version-less by design


def test_memory_snippet_matches_the_readme_block():
    # README.md is the authority for this blurb - it is the copy users read while
    # onboarding - and MEMORY_SNIPPET mirrors it so the CLI can print it without
    # shipping the README. Editing one alone would put two different blurbs into
    # users' memory files, so drift fails here rather than going unnoticed.
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    blocks = re.findall(r"^```markdown\n(.*?)^```$", readme, re.DOTALL | re.MULTILINE)
    assert len(blocks) == 1, "expected exactly one ```markdown block in README.md"
    assert blocks[0].rstrip("\n") == guide.MEMORY_SNIPPET, (
        "README.md and guide.MEMORY_SNIPPET have drifted. README.md is the "
        "authority: copy its ```markdown block into MEMORY_SNIPPET verbatim."
    )


def test_guide_json_carries_memory_snippet_in_every_state(cwd):
    # Available to programmatic consumers, cold and with an active project.
    cold = json.loads(runner.invoke(app, ["guide", "--json"]).output)
    assert cold["memory_snippet"] == guide.MEMORY_SNIPPET

    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    warm = json.loads(runner.invoke(app, ["guide", "--json"]).output)
    assert warm["memory_snippet"] == guide.MEMORY_SNIPPET


def test_guide_shows_you_are_here_for_active_project(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["guide"])
    assert result.exit_code == 0
    assert "my-thing" in result.output
    assert "next" in result.output.lower()

    data = json.loads(runner.invoke(app, ["guide", "--json"]).output)
    assert data["active_project"] == "my-thing"
    assert data["next_action"] == "brainstorm"
