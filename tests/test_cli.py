import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import config
from specflo.cli import app

runner = CliRunner()


@pytest.fixture
def cwd(monkeypatch, tmp_path):
    """Run each command with the tmp dir as the working directory."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_init_scaffolds_config_and_projects_dir(cwd):
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (cwd / ".specflo" / "config.yaml").is_file()
    assert (cwd / "docs" / "projects").is_dir()


def test_init_twice_fails(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["init"])
    assert result.exit_code != 0


def test_new_creates_project_and_makes_it_active(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["new", "My Thing"])
    assert result.exit_code == 0
    assert (cwd / "docs" / "projects" / "my-thing" / "project.md").is_file()
    assert config.load_config(cwd).active_project == "my-thing"


def test_new_without_init_fails(cwd):
    result = runner.invoke(app, ["new", "My Thing"])
    assert result.exit_code != 0


def test_status_without_init_is_friendly(cwd):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "init" in result.output.lower()

    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["initialized"] is False


def test_status_with_no_active_project(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "new" in result.output.lower()

    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["initialized"] is True
    assert data["active_project"] is None


def test_status_reports_the_active_project_for_humans(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "My Thing" in result.output
    assert "brainstorm" in result.output


def test_unknown_command_shows_full_help_not_just_a_hint():
    result = runner.invoke(app, ["bogus"])
    assert result.exit_code != 0
    out = result.output
    assert "bogus" in out  # names the offending command
    assert "Commands" in out  # the full help (command list) is shown
    assert "status" in out
    assert "--help' for help" not in out  # the bare "Try ... --help" hint is gone


def test_new_help_describes_the_name_argument():
    result = runner.invoke(app, ["new", "--help"])
    assert result.exit_code == 0
    assert "<name>" in result.output  # argument metavar
    assert "slug" in result.output.lower()  # argument is described


def test_top_level_help_shows_usage_examples():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Example" in result.output


def test_top_level_help_lists_new_with_its_argument():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "new <name>" in result.output


def test_top_level_help_does_not_add_args_to_argless_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # init and status take no positional arguments
    assert "init <" not in result.output
    assert "status <" not in result.output


def test_status_collapses_the_label_when_name_equals_slug(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "tpro"])  # name == slug == "tpro"
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Project: tpro" in result.output
    assert "tpro (tpro)" not in result.output


def test_status_shows_name_and_slug_when_they_differ(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["status"])
    assert "My Thing (my-thing)" in result.output


def test_status_shows_the_project_directory(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["status"])
    assert "docs/projects/my-thing" in result.output


def test_status_json_includes_the_project_directory(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["dir"].endswith("docs/projects/my-thing")
    assert Path(data["dir"]).is_absolute()


def test_status_reports_the_active_project_as_json(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["active_project"] == "my-thing"
    assert data["name"] == "My Thing"
    assert data["phase"] == "brainstorm"
    assert data["next_phase"] == "spec"
    assert data["next_step"]


def test_list_shows_projects_and_marks_the_active_one(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo"])  # Bravo is now active

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "bravo" in result.output
    # The active project (bravo) is marked; alpha is not.
    active_line = next(line for line in result.output.splitlines() if "bravo" in line)
    other_line = next(line for line in result.output.splitlines() if "alpha" in line)
    assert "*" in active_line
    assert "*" not in other_line


def test_list_shows_each_project_phase(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "brainstorm" in result.output


def test_list_without_projects_is_friendly(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "new" in result.output.lower()


def test_list_without_init_fails(cwd):
    result = runner.invoke(app, ["list"])
    assert result.exit_code != 0


def test_list_json_output(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo"])  # active

    data = json.loads(runner.invoke(app, ["list", "--json"]).output)
    assert data["active_project"] == "bravo"
    slugs = [p["slug"] for p in data["projects"]]
    assert slugs == ["alpha", "bravo"]
    by_slug = {p["slug"]: p for p in data["projects"]}
    assert by_slug["bravo"]["active"] is True
    assert by_slug["alpha"]["active"] is False
    assert by_slug["alpha"]["phase"] == "brainstorm"


def test_switch_changes_the_active_project(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo"])  # Bravo is active

    result = runner.invoke(app, ["switch", "Alpha"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert config.load_config(cwd).active_project == "alpha"


def test_switch_to_a_missing_project_fails(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["switch", "ghost"])
    assert result.exit_code != 0
    assert config.load_config(cwd).active_project is None


def test_switch_without_init_fails(cwd):
    result = runner.invoke(app, ["switch", "anything"])
    assert result.exit_code != 0


def test_top_level_help_lists_switch_with_its_argument():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "switch <name>" in result.output


def test_brainstorm_start_creates_the_artifact(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["brainstorm", "start"])
    assert result.exit_code == 0
    assert (cwd / "docs" / "projects" / "my-thing" / "brainstorm.md").is_file()
    assert "brainstorm.md" in result.output


def test_brainstorm_start_is_resume_friendly(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])
    result = runner.invoke(app, ["brainstorm", "start"])
    assert result.exit_code == 0
    assert "already started" in result.output.lower()


def test_brainstorm_start_without_active_project_fails(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["brainstorm", "start"])
    assert result.exit_code != 0


def test_brainstorm_start_json(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["brainstorm", "start", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["created"] is True
    assert data["path"].endswith("brainstorm.md")


def test_decision_add_records_an_id(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])
    result = runner.invoke(
        app, ["decision", "add", "--text", "Use SQLite", "--rationale", "simplest"]
    )
    assert result.exit_code == 0
    assert "D-01" in result.output
    text = (cwd / "docs" / "projects" / "my-thing" / "brainstorm.md").read_text()
    assert "### D-01 — Use SQLite" in text


def test_decision_add_supersede(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])
    runner.invoke(app, ["decision", "add", "--text", "Use JSON"])
    result = runner.invoke(
        app, ["decision", "add", "--text", "Use YAML", "--supersedes", "D-01"]
    )
    assert result.exit_code == 0
    assert "D-02" in result.output
    text = (cwd / "docs" / "projects" / "my-thing" / "brainstorm.md").read_text()
    assert "superseded by D-02" in text


def test_decision_add_without_start_fails(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["decision", "add", "--text", "Too early"])
    assert result.exit_code != 0


def test_validate_brainstorm_reports_issues_and_exits_nonzero(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])
    result = runner.invoke(app, ["validate", "brainstorm"])
    assert result.exit_code != 0  # fresh doc: no decisions + empty out-of-scope
    lowered = result.output.lower()
    assert "no decisions" in lowered or "out of scope" in lowered


def test_validate_brainstorm_passes_a_complete_doc(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])
    runner.invoke(app, ["decision", "add", "--text", "A decision"])
    path = cwd / "docs" / "projects" / "my-thing" / "brainstorm.md"
    path.write_text(
        path.read_text().replace(
            "## Out of scope / Deferred\n"
            "<!-- required, must be non-empty before validate passes -->",
            "## Out of scope / Deferred\nNo auth in v0.1.",
        )
    )
    result = runner.invoke(app, ["validate", "brainstorm"])
    assert result.exit_code == 0
    assert "ok" in result.output.lower()


def test_validate_unknown_artifact_fails(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["validate", "bogus"])
    assert result.exit_code != 0


def test_validate_json_reports_ready(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])
    result = runner.invoke(app, ["validate", "brainstorm", "--json"])
    assert result.exit_code == 1  # fresh/not-ready doc must exit non-zero
    data = json.loads(result.output)
    assert data["ready"] is False
    assert data["issues"]


def _ready_brainstorm(cwd):
    """init + new + a brainstorm that passes `validate brainstorm`."""
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])
    runner.invoke(app, ["decision", "add", "--text", "A decision"])
    path = cwd / "docs" / "projects" / "my-thing" / "brainstorm.md"
    path.write_text(
        path.read_text().replace(
            "## Out of scope / Deferred\n"
            "<!-- required, must be non-empty before validate passes -->",
            "## Out of scope / Deferred\nNo auth in v0.1.",
        )
    )
    return path


def _ready_spec(cwd):
    """A project advanced to the spec phase with a spec.md that passes validate."""
    _ready_brainstorm(cwd)
    runner.invoke(app, ["advance"])  # brainstorm -> spec
    runner.invoke(app, ["spec", "start"])
    runner.invoke(app, ["requirement", "add", "--text", "A req", "--acceptance", "it passes"])
    path = cwd / "docs" / "projects" / "my-thing" / "spec.md"
    text = path.read_text()
    text = text.replace(
        "### In scope\n<!-- required, non-empty -->",
        "### In scope\n- the CLI.",
    ).replace(
        "### Out of scope\n"
        "<!-- required, non-empty; carried from the brainstorm's Out of scope / Deferred -->",
        "### Out of scope\n- the GUI.",
    )
    path.write_text(text)
    return path


def test_advance_moves_a_ready_brainstorm_to_spec(cwd):
    brainstorm_md = _ready_brainstorm(cwd)
    result = runner.invoke(app, ["advance"])
    assert result.exit_code == 0
    assert "spec" in result.output
    project_md = cwd / "docs" / "projects" / "my-thing" / "project.md"
    assert "phase: spec" in project_md.read_text()
    assert "status: complete" in brainstorm_md.read_text()


def test_advance_refuses_a_not_ready_brainstorm_without_mutating(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])  # fresh: no decisions, empty out-of-scope
    result = runner.invoke(app, ["advance"])
    assert result.exit_code != 0
    project_md = cwd / "docs" / "projects" / "my-thing" / "project.md"
    brainstorm_md = cwd / "docs" / "projects" / "my-thing" / "brainstorm.md"
    assert "phase: brainstorm" in project_md.read_text()  # phase unchanged
    assert "status: draft" in brainstorm_md.read_text()  # artifact untouched


def test_advance_moves_a_ready_spec_to_plan(cwd):
    spec_md = _ready_spec(cwd)
    result = runner.invoke(app, ["advance"])
    assert result.exit_code == 0
    assert "plan" in result.output
    project_md = cwd / "docs" / "projects" / "my-thing" / "project.md"
    assert "phase: plan" in project_md.read_text()
    assert "status: complete" in spec_md.read_text()


def test_advance_refuses_a_not_ready_spec_without_mutating(cwd):
    _ready_brainstorm(cwd)
    runner.invoke(app, ["advance"])  # brainstorm -> spec
    runner.invoke(app, ["spec", "start"])  # fresh spec: no requirements, empty boundaries
    result = runner.invoke(app, ["advance"])
    assert result.exit_code != 0
    project_md = cwd / "docs" / "projects" / "my-thing" / "project.md"
    spec_md = cwd / "docs" / "projects" / "my-thing" / "spec.md"
    assert "phase: spec" in project_md.read_text()   # phase unchanged
    assert "status: draft" in spec_md.read_text()     # artifact untouched


def test_advance_without_active_project_fails(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["advance"])
    assert result.exit_code != 0


def test_advance_past_the_final_phase_fails(cwd):
    _ready_spec(cwd)
    runner.invoke(app, ["advance"])  # spec -> plan (gated; spec is ready)
    runner.invoke(app, ["advance"])  # plan -> execute (ungated)
    result = runner.invoke(app, ["advance"])  # execute is final
    assert result.exit_code != 0
    assert "final" in result.output.lower()


def test_advance_json_success_shape(cwd):
    _ready_brainstorm(cwd)
    result = runner.invoke(app, ["advance", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["advanced"] is True
    assert data["from"] == "brainstorm"
    assert data["to"] == "spec"
    assert data["checkpoint"].endswith("checkpoint.md")


def test_advance_json_gate_failure_shape(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])  # not ready
    result = runner.invoke(app, ["advance", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["advanced"] is False
    assert data["issues"]


def test_spec_start_creates_the_artifact(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["spec", "start"])
    assert result.exit_code == 0
    assert (cwd / "docs" / "projects" / "my-thing" / "spec.md").is_file()
    assert "spec.md" in result.output


def test_spec_start_is_resume_friendly(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["spec", "start"])
    result = runner.invoke(app, ["spec", "start"])
    assert result.exit_code == 0
    assert "already started" in result.output.lower()


def test_spec_start_without_active_project_fails(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["spec", "start"])
    assert result.exit_code != 0


def test_spec_start_json(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    data = json.loads(runner.invoke(app, ["spec", "start", "--json"]).output)
    assert data["created"] is True
    assert data["path"].endswith("spec.md")


def test_requirement_add_records_an_id(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["spec", "start"])
    result = runner.invoke(
        app,
        ["requirement", "add", "--text", "Prints help", "--acceptance", "no-arg run exits 0"],
    )
    assert result.exit_code == 0
    assert "REQ-01" in result.output
    text = (cwd / "docs" / "projects" / "my-thing" / "spec.md").read_text()
    assert "### REQ-01 — Prints help" in text
    assert "- Acceptance: no-arg run exits 0" in text


def test_requirement_add_acceptance_is_required(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["spec", "start"])
    result = runner.invoke(app, ["requirement", "add", "--text", "No acceptance given"])
    assert result.exit_code != 0  # missing required --acceptance


def test_requirement_add_from_links_a_decision(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])
    runner.invoke(app, ["decision", "add", "--text", "Use SQLite"])  # D-01
    runner.invoke(app, ["spec", "start"])
    result = runner.invoke(
        app,
        ["requirement", "add", "--text", "Persist to SQLite",
         "--acceptance", "survives restart", "--from", "D-01"],
    )
    assert result.exit_code == 0
    text = (cwd / "docs" / "projects" / "my-thing" / "spec.md").read_text()
    assert "- Derives from: D-01" in text


def test_requirement_add_from_unknown_decision_fails(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])
    runner.invoke(app, ["spec", "start"])
    result = runner.invoke(
        app,
        ["requirement", "add", "--text", "x", "--acceptance", "y", "--from", "D-99"],
    )
    assert result.exit_code != 0


def test_requirement_add_supersede(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["spec", "start"])
    runner.invoke(app, ["requirement", "add", "--text", "old", "--acceptance", "a"])
    result = runner.invoke(
        app,
        ["requirement", "add", "--text", "new", "--acceptance", "b", "--supersedes", "REQ-01"],
    )
    assert result.exit_code == 0
    assert "REQ-02" in result.output
    text = (cwd / "docs" / "projects" / "my-thing" / "spec.md").read_text()
    assert "superseded by REQ-02" in text


def test_requirement_add_without_start_fails(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["requirement", "add", "--text", "x", "--acceptance", "y"])
    assert result.exit_code != 0


def test_validate_spec_reports_issues_and_exits_nonzero(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["spec", "start"])
    result = runner.invoke(app, ["validate", "spec"])
    assert result.exit_code != 0  # fresh doc: no requirements + empty boundaries
    assert "no requirements" in result.output.lower() or "scope" in result.output.lower()


def test_validate_spec_passes_a_complete_doc(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["spec", "start"])
    runner.invoke(app, ["requirement", "add", "--text", "A req", "--acceptance", "it passes"])
    path = cwd / "docs" / "projects" / "my-thing" / "spec.md"
    text = path.read_text()
    text = text.replace(
        "### In scope\n<!-- required, non-empty -->",
        "### In scope\n- the CLI.",
    ).replace(
        "### Out of scope\n"
        "<!-- required, non-empty; carried from the brainstorm's Out of scope / Deferred -->",
        "### Out of scope\n- the GUI.",
    )
    path.write_text(text)
    result = runner.invoke(app, ["validate", "spec"])
    assert result.exit_code == 0
    assert "ok" in result.output.lower()


def test_validate_spec_json_reports_not_ready(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["spec", "start"])
    data = json.loads(runner.invoke(app, ["validate", "spec", "--json"]).output)
    assert data["ready"] is False
    assert data["issues"]


def test_checkpoint_prints_the_resume_prompt(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["checkpoint"])
    assert result.exit_code == 0
    assert "Checkpoint" in result.output
    assert "Read first" in result.output
    assert "my-thing" in result.output


def test_checkpoint_writes_the_file(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["checkpoint"])
    path = cwd / "docs" / "projects" / "my-thing" / "checkpoint.md"
    assert path.is_file()
    assert "phase: brainstorm" in path.read_text()


def test_checkpoint_json(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    data = json.loads(runner.invoke(app, ["checkpoint", "--json"]).output)
    assert data["project"] == "my-thing"
    assert data["phase"] == "brainstorm"
    assert data["read_first"][0].endswith("project.md")
    assert data["path"].endswith("checkpoint.md")


def test_checkpoint_without_active_project_fails(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["checkpoint"])
    assert result.exit_code != 0


def test_new_creates_an_initial_checkpoint(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    path = cwd / "docs" / "projects" / "my-thing" / "checkpoint.md"
    assert path.is_file()
    assert "phase: brainstorm" in path.read_text()


def test_decision_add_refreshes_the_checkpoint(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])
    runner.invoke(app, ["decision", "add", "--text", "Use SQLite"])
    path = cwd / "docs" / "projects" / "my-thing" / "checkpoint.md"
    text = path.read_text()
    # brainstorm.md now exists, so it is listed in "Read first"
    assert "brainstorm.md" in text


def test_mutation_survives_a_failing_checkpoint_refresh(cwd, monkeypatch):
    """A crash inside the silent checkpoint refresh must never fail its host command.

    The auto-refresh runs AFTER the mutation has already persisted, so an OSError
    from write_checkpoint (read-only FS, permissions, disk full, ...) must be
    swallowed — the command still exits 0 and its mutation stands.
    """
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    runner.invoke(app, ["brainstorm", "start"])

    def _raise_oserror(*args, **kwargs):
        raise OSError("read-only file system")

    # cli.py calls checkpoint.write_checkpoint(...) by attribute at call time.
    monkeypatch.setattr("specflo.checkpoint.write_checkpoint", _raise_oserror)

    result = runner.invoke(app, ["decision", "add", "--text", "Use SQLite"])
    assert result.exit_code == 0          # the refresh failure did not crash the command
    assert "D-01" in result.output        # the mutation's normal success output is present
    # and the mutation itself persisted to brainstorm.md
    text = (cwd / "docs" / "projects" / "my-thing" / "brainstorm.md").read_text()
    assert "### D-01 — Use SQLite" in text


def test_requirement_add_refreshes_the_checkpoint(cwd):
    _ready_brainstorm(cwd)
    runner.invoke(app, ["advance"])      # brainstorm -> spec
    runner.invoke(app, ["spec", "start"])
    runner.invoke(app, ["requirement", "add", "--text", "A req", "--acceptance", "it passes"])
    path = cwd / "docs" / "projects" / "my-thing" / "checkpoint.md"
    text = path.read_text()
    assert "phase: spec" in text
    assert "spec.md" in text


def test_advance_writes_checkpoint_and_points_to_it(cwd):
    _ready_brainstorm(cwd)
    result = runner.invoke(app, ["advance"])
    assert result.exit_code == 0
    assert "Checkpoint saved" in result.output
    assert "specflo checkpoint" in result.output
    path = cwd / "docs" / "projects" / "my-thing" / "checkpoint.md"
    assert path.is_file()
    assert "phase: spec" in path.read_text()   # reflects the NEW phase


def test_advance_json_includes_the_checkpoint_path(cwd):
    _ready_brainstorm(cwd)
    data = json.loads(runner.invoke(app, ["advance", "--json"]).output)
    assert data["advanced"] is True
    assert data["checkpoint"].endswith("checkpoint.md")


def test_status_points_to_the_checkpoint_for_humans(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["status"])
    assert "Resume:" in result.output
    assert "specflo checkpoint" in result.output


def test_status_json_includes_the_checkpoint_path(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["checkpoint"].endswith("checkpoint.md")


def test_checkpoint_lifecycle_smoke(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    cp = cwd / "docs" / "projects" / "my-thing" / "checkpoint.md"

    # new wrote an initial brainstorm-phase checkpoint
    assert "phase: brainstorm" in cp.read_text()

    # a decision refreshes it; brainstorm.md now appears
    runner.invoke(app, ["brainstorm", "start"])
    runner.invoke(app, ["decision", "add", "--text", "Use SQLite"])
    assert "brainstorm.md" in cp.read_text()

    # advancing to spec rewrites it for the new phase
    _ready_brainstorm(cwd)                 # fills out-of-scope so advance passes
    runner.invoke(app, ["advance"])
    assert "phase: spec" in cp.read_text()

    # the command reprints the current prompt
    out = runner.invoke(app, ["checkpoint"]).output
    assert "phase: spec" in out
