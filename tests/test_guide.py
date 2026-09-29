import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import config, guide, projects
from specflo.cli import app, build_cli

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
    assert data["next_action"] == "none"
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
    # useful, falls back to the no-active guidance rather than blowing up.
    cfg = config.init_config(tmp_path)
    cfg.active_project = "ghost"
    data = guide.build_guide(tmp_path, cfg)
    assert data["initialized"] is True
    assert data["next_action"] == "none"


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
    # build_cli, not the Typer app: it mounts the `skills` group too.
    cli = build_cli()
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


def test_guide_with_no_active_project_is_neutral(cwd):
    # No active project is a state, not a prompt to create one: the guide names
    # both ways in (new, switch) and nudges toward neither.
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["guide"])
    assert result.exit_code == 0
    assert "No active project" in result.output
    assert "specflo new" in result.output
    assert "specflo switch" in result.output
    assert "to start one" not in result.output
    assert "Create one" not in result.output


def test_guide_json_no_active_project_next_action_is_none(cwd):
    runner.invoke(app, ["init"])
    data = json.loads(runner.invoke(app, ["guide", "--json"]).output)
    assert data["initialized"] is True
    assert data["active_project"] is None
    assert data["next_action"] == "none"


def test_guide_tells_the_agent_to_show_a_follow_up_the_user_names(cwd):
    # A user who opens with "do FU-92" gets the entry read through the CLI,
    # before a level is proposed, not found with a text search.
    result = runner.invoke(app, ["guide"])
    assert result.exit_code == 0
    line = next(line for line in result.output.splitlines() if "(FU-NN)" in line)
    assert "specflo followup show FU-NN" in line
    assert "before you propose a level" in line


def test_guide_table_names_what_closes_a_follow_up():
    entries = {entry["name"]: entry for entry in guide.COMMANDS}
    assert "--by <ref>" in entries["followup close"]["args"]
    assert "Closed by" in entries["followup show"]["summary"]
    assert "--closes <FU-NN>" in entries["task done"]["args"]


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


# --- the prior-projects rule line (project-index REQ-06) -----------------


def test_guide_shows_the_rule_line_with_completed_projects(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    cfg = config.load_config(cwd)
    projects.complete_project(cwd, cfg, "my-thing")

    out = runner.invoke(app, ["guide"]).output
    assert config.rule_text("historical") in out

    runner.invoke(app, ["config", "set", "prior_projects", "binding"])
    out = runner.invoke(app, ["guide"]).output
    assert config.rule_text("binding") in out
    assert config.rule_text("historical") not in out


def test_guide_omits_the_rule_line_without_completed_projects(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    out = runner.invoke(app, ["guide"]).output
    assert config.rule_text("historical") not in out


# --- the review loop in the guide and the README ------------------------------------------

_REVIEW_COMMANDS = ("review finding add", "review finding check", "review waive", "review prompt")


def test_guide_lists_the_review_loop_commands(cwd):
    out = runner.invoke(app, ["guide"]).output
    for command in _REVIEW_COMMANDS:
        assert command in out, command


def test_guide_lists_the_harden_level_with_its_meaning(cwd):
    out = runner.invoke(app, ["guide"]).output
    line = next(line for line in out.splitlines() if line.startswith("  harden "))
    assert "`--level harden`" in line
    for phrase in ("code that already exists", "harden rounds", "fix tasks"):
        assert phrase in line, phrase
    entries = {entry["name"]: entry for entry in guide.COMMANDS}
    assert "harden" in entries["new"]["args"]


def test_guide_lists_the_harden_loop_commands(cwd):
    entries = {entry["name"]: entry for entry in guide.COMMANDS}
    assert "--harden" in entries["review start"]["args"]
    assert "--at <file>:<line>[-<line>]" in entries["review finding add"]["args"]
    assert "--fixes <F-NN>" in entries["task add"]["args"]
    assert "--do <what>" in entries["review finding defer"]["args"]
    assert "--reason <why>" in entries["review finding reject"]["args"]
    out = runner.invoke(app, ["guide"]).output
    for label in ("review start [--full] [--over-budget] [--harden]", "review finding add",
                  "task add", "review finding defer", "review finding reject"):
        assert f"    {label}" in out, label
    assert "--fixes <F-NN>" in out and "--at <file>:<line>[-<line>]" in out


def _daemon_names():
    return [e["name"] for e in guide.COMMANDS if e["group"] == "daemon"]


def test_guide_rows_do_not_pad_to_the_longest_label(cwd):
    out = runner.invoke(app, ["guide"]).output
    rows = out[out.index("Commands:"):out.index("Skills:")].splitlines()
    assert all(len(row) <= 100 for row in rows), max(rows, key=len)
    daemon = runner.invoke(app, ["guide", "daemon"]).output.splitlines()
    assert all(len(row) <= 100 for row in daemon), max(daemon, key=len)


def test_guide_moves_the_daemon_commands_to_their_own_topic(cwd):
    out = runner.invoke(app, ["guide"]).output
    for name in _daemon_names():
        assert f"    {name} " not in out, name
    assert "`specflo guide daemon`" in out
    result = runner.invoke(app, ["guide", "daemon"])
    assert result.exit_code == 0
    for name in _daemon_names():
        assert f"    {name} " in result.output, name
    assert "    task add" not in result.output
    names = set(_daemon_names())
    for prefix in ("serve", "remote", "promote", "product", "workitem", "gate", "lease",
                   "console"):
        assert any(n.split()[0] == prefix for n in names), prefix


def test_guide_lists_the_commands_in_workflow_order(cwd):
    out = runner.invoke(app, ["guide"]).output
    headings = ["Setup", "Projects", "Brainstorm", "Spec", "Plan", "Execute", "Review",
                "Any phase", "Agents"]
    at = [out.index(f"\n  {h}\n") for h in headings]
    assert at == sorted(at)
    commands = ["init", "skills install", "hook install", "config get", "new", "status",
                "brainstorm start", "decision add", "spec start", "requirement add",
                "plan start", "task add", "plan graph", "task list", "task start", "task done",
                "review start", "review done", "validate", "advance", "followup add",
                "agent start"]
    at = [out.index(f"\n    {c} ") for c in commands]
    assert at == sorted(at)
    assert "FU-\n" not in out


def test_guide_json_keeps_every_command(cwd):
    data = json.loads(runner.invoke(app, ["guide", "--json"]).output)
    assert len(data["commands"]) == len(guide.COMMANDS)


def test_guide_refuses_an_unknown_topic(cwd):
    result = runner.invoke(app, ["guide", "bogus"])
    assert result.exit_code == 1
    assert "daemon" in result.output


def test_readme_documents_the_review_loop():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    for term in (*_REVIEW_COMMANDS, "--full", "--over-budget", "review_max_rounds",
                 "review-budget"):
        assert term in readme, term
