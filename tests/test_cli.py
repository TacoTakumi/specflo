import json
import json as _json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import config
from specflo.cli import app

runner = CliRunner()


def _new_project_with_spec(runner, app):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Thing"])
    runner.invoke(app, ["brainstorm", "start"])
    runner.invoke(app, ["decision", "add", "--text", "Use SQLite"])
    runner.invoke(app, ["advance"])  # brainstorm -> spec (validate brainstorm passes by default)
    runner.invoke(app, ["spec", "start"])
    runner.invoke(app, ["requirement", "add", "--text", "store data",
                        "--acceptance", "data survives restart", "--from", "D-01"])


def test_plan_start_and_task_add(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _new_project_with_spec(runner, app)
    # advance spec -> plan first (fill boundaries so validate spec passes)
    spec_md = tmp_path / "docs" / "projects" / "thing" / "spec.md"
    spec_md.write_text(spec_md.read_text()
                       .replace("### In scope\n<!-- required, non-empty -->",
                                "### In scope\n- the thing.")
                       .replace("### Out of scope\n"
                                "<!-- required, non-empty; carried from the brainstorm's Out of scope / Deferred -->",
                                "### Out of scope\n- other things."))
    runner.invoke(app, ["advance"])  # spec -> plan
    r = runner.invoke(app, ["plan", "start"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["task", "add", "--text", "build it",
                            "--acceptance", "it works", "--verify", "uv run pytest",
                            "--from", "REQ-01", "--json"])
    assert r.exit_code == 0
    data = _json.loads(r.output)
    assert data["id"] == "T-01"
    assert data["implements"] == ["REQ-01"]


def test_task_add_requires_from(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _new_project_with_spec(runner, app)
    runner.invoke(app, ["plan", "start"])  # phase still spec, but start is allowed once active
    r = runner.invoke(app, ["task", "add", "--text", "x",
                            "--acceptance", "a", "--verify", "v"])
    assert r.exit_code != 0  # --from is required


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


def test_new_scaffolds_brainstorm(cwd):
    """`new` leaves the project ready to work: brainstorm.md is scaffolded (REQ-01)."""
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["new", "My Thing"])
    assert result.exit_code == 0
    bs = cwd / "docs" / "projects" / "my-thing" / "brainstorm.md"
    assert bs.is_file()
    text = bs.read_text()
    for header in (
        "## Current understanding",
        "## Decisions",
        "## Out of scope / Deferred",
        "## Open questions",
    ):
        assert header in text


def test_new_output_names_brainstorm_path(cwd):
    """`new`'s output is self-sufficient: it names the scaffolded brainstorm.md (REQ-04)."""
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["new", "My Thing"])
    assert result.exit_code == 0
    out = result.output
    assert "my-thing" in out  # the slug
    assert "now active" in out  # the existing line is retained
    assert "brainstorm" in out
    assert "my-thing/brainstorm.md" in out  # the scaffolded artifact, by path


def test_advance_does_not_scaffold(cwd):
    """Regression-lock: advance creates no phase artifact; spec.md appears only via spec start (REQ-02)."""
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    # Make the brainstorm valid so advance proceeds (a decision + non-empty out-of-scope).
    runner.invoke(app, ["decision", "add", "--text", "Use SQLite"])
    bs = cwd / "docs" / "projects" / "my-thing" / "brainstorm.md"
    bs.write_text(
        bs.read_text().replace(
            "## Out of scope / Deferred\n"
            "<!-- required, must be non-empty before validate passes -->",
            "## Out of scope / Deferred\n- nothing else.",
        )
    )
    r = runner.invoke(app, ["advance"])  # brainstorm -> spec
    assert r.exit_code == 0
    spec_md = cwd / "docs" / "projects" / "my-thing" / "spec.md"
    assert not spec_md.exists()  # advance scaffolds nothing
    runner.invoke(app, ["spec", "start"])
    assert spec_md.is_file()  # the artifact appears only via its own start


def test_brainstorm_start_idempotent_after_new(cwd):
    """Regression-lock: after `new`, `brainstorm start` locates the file, reports
    already-started, and leaves it byte-for-byte unchanged (REQ-05)."""
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    bs = cwd / "docs" / "projects" / "my-thing" / "brainstorm.md"
    before = bs.read_bytes()
    result = runner.invoke(app, ["brainstorm", "start"])
    assert result.exit_code == 0
    assert "already started" in result.output
    assert bs.read_bytes() == before  # non-destructive


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
    # `new` auto-scaffolds brainstorm.md; remove it to exercise start's create path.
    (cwd / "docs" / "projects" / "my-thing" / "brainstorm.md").unlink()
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


def test_decision_add_without_brainstorm_fails(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    # `new` auto-scaffolds brainstorm.md; remove it to exercise the missing-file guard.
    (cwd / "docs" / "projects" / "my-thing" / "brainstorm.md").unlink()
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


def test_advance_at_execute_refused_when_tasks_pending(cwd):
    _ready_spec(cwd)
    runner.invoke(app, ["advance"])  # spec -> plan (gated; spec is ready)
    # plan -> execute is now gated: need a valid plan with at least one task.
    runner.invoke(app, ["plan", "start"])
    runner.invoke(app, ["task", "add", "--text", "do it",
                        "--acceptance", "it works", "--verify", "uv run pytest",
                        "--from", "REQ-01"])
    runner.invoke(app, ["advance"])  # plan -> execute (gated; plan is ready)
    result = runner.invoke(app, ["advance"])  # execute is terminal — refused (task pending)
    assert result.exit_code != 0
    assert "not all tasks are done" in result.output.lower()


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


def _project_at_execute(runner, app, tmp_path):
    """Drive a fresh project to the execute phase with one pending task T-01."""
    _new_project_with_spec(runner, app)
    # Fill brainstorm OOS so advance brainstorm->spec passes.
    bs_md = tmp_path / "docs" / "projects" / "thing" / "brainstorm.md"
    bs_md.write_text(bs_md.read_text().replace(
        "## Out of scope / Deferred\n"
        "<!-- required, must be non-empty before validate passes -->",
        "## Out of scope / Deferred\nNo auth in v0.1.",
    ))
    runner.invoke(app, ["advance"])                 # brainstorm -> spec
    spec_md = tmp_path / "docs" / "projects" / "thing" / "spec.md"
    spec_md.write_text(spec_md.read_text()
        .replace("### In scope\n<!-- required, non-empty -->",
                 "### In scope\n- the thing.")
        .replace("### Out of scope\n"
                 "<!-- required, non-empty; carried from the brainstorm's "
                 "Out of scope / Deferred -->",
                 "### Out of scope\n- other things."))
    runner.invoke(app, ["advance"])                 # spec -> plan
    runner.invoke(app, ["plan", "start"])
    runner.invoke(app, ["task", "add", "--text", "build it", "--acceptance",
                        "it works", "--verify", "true", "--from", "REQ-01"])  # T-01
    runner.invoke(app, ["advance"])                 # plan -> execute


def _project_at_plan_phase(runner, app, tmp_path):
    _new_project_with_spec(runner, app)
    # _new_project_with_spec leaves us at brainstorm phase (OOS empty, advance fails).
    # Fill brainstorm OOS so advance brainstorm->spec passes, then advance through spec.
    bs_md = tmp_path / "docs" / "projects" / "thing" / "brainstorm.md"
    bs_md.write_text(bs_md.read_text().replace(
        "## Out of scope / Deferred\n"
        "<!-- required, must be non-empty before validate passes -->",
        "## Out of scope / Deferred\nNo auth in v0.1.",
    ))
    runner.invoke(app, ["advance"])  # brainstorm -> spec
    # spec.md already has REQ-01 from _new_project_with_spec; fill boundaries.
    spec_md = tmp_path / "docs" / "projects" / "thing" / "spec.md"
    spec_md.write_text(spec_md.read_text()
                       .replace("### In scope\n<!-- required, non-empty -->",
                                "### In scope\n- the thing.")
                       .replace("### Out of scope\n"
                                "<!-- required, non-empty; carried from the brainstorm's Out of scope / Deferred -->",
                                "### Out of scope\n- other things."))
    runner.invoke(app, ["advance"])  # spec -> plan
    runner.invoke(app, ["plan", "start"])


def test_validate_plan_reports_coverage_gap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    r = runner.invoke(app, ["validate", "plan", "--json"])
    data = _json.loads(r.output)
    assert data["ready"] is False  # no tasks yet -> REQ uncovered / no tasks
    assert r.exit_code == 1


def test_validate_plan_warnings_do_not_fail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "ship a stub for now",
                        "--acceptance", "returns a value", "--verify", "uv run pytest",
                        "--from", "REQ-01"])
    r = runner.invoke(app, ["validate", "plan", "--json"])
    data = _json.loads(r.output)
    assert data["ready"] is True          # warnings don't block
    assert any("stub" in w for w in data["warnings"])
    assert r.exit_code == 0


def test_advance_plan_to_execute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "build it",
                        "--acceptance", "it works", "--verify", "uv run pytest",
                        "--from", "REQ-01"])
    r = runner.invoke(app, ["advance", "--json"])
    data = _json.loads(r.output)
    assert data["advanced"] is True
    assert data["from"] == "plan" and data["to"] == "execute"
    assert "T-01" in data["next_step"]       # progress-aware: names the first actionable task
    plan_md = (tmp_path / "docs" / "projects" / "thing" / "plan.md").read_text()
    assert "status: complete" in plan_md


def test_advance_into_execute_next_step_is_progress_aware(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "build it", "--acceptance",
                        "it works", "--verify", "true", "--from", "REQ-01"])
    out = runner.invoke(app, ["advance"]).output          # human, plan -> execute
    assert "T-01" in out and "task show" in out           # the "Next:" line points at the first task
    assert "may clear context" in out.lower()             # permissive clear-context affordance
    assert "specflo checkpoint" in out                    # ...paired with the resume command


def test_advance_completion_offers_clear_context_affordance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_execute(runner, app, tmp_path)            # at execute, T-01 pending
    runner.invoke(app, ["task", "start", "T-01"])
    runner.invoke(app, ["task", "done", "T-01"])
    out = runner.invoke(app, ["advance"]).output          # human, completes the project
    assert "Completed project" in out
    assert "may clear context" in out.lower()             # the final phase-end gets it too


def test_task_progress_verbs_and_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "build it",
                        "--acceptance", "it works", "--verify", "uv run pytest",
                        "--from", "REQ-01"])
    r = runner.invoke(app, ["task", "start", "T-01", "--json"])
    assert _json.loads(r.output)["progress"] == "in_progress"
    r = runner.invoke(app, ["task", "done", "T-01"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["task", "list", "--json"])
    data = _json.loads(r.output)
    assert data["tasks"][0]["progress"] == "done"
    assert data["progress"]["all_done"] is True


def test_task_block_records_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "build it",
                        "--acceptance", "it works", "--verify", "uv run pytest",
                        "--from", "REQ-01"])
    r = runner.invoke(app, ["task", "block", "T-01", "--reason", "waiting on API"])
    assert r.exit_code == 0
    plan_md = (tmp_path / "docs" / "projects" / "thing" / "plan.md").read_text()
    assert "- Progress: blocked" in plan_md
    assert "- Blocked: waiting on API" in plan_md


def test_task_rewire_repoints_dependents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "base", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01"])                       # T-01
    runner.invoke(app, ["task", "add", "--text", "dependent", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01", "--depends-on", "T-01"])  # T-02
    runner.invoke(app, ["task", "add", "--text", "replacement", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01"])                       # T-03
    r = runner.invoke(app, ["task", "rewire", "--from", "T-01", "--to", "T-03", "--json"])
    assert r.exit_code == 0
    assert _json.loads(r.output)["rewired"] == ["T-02"]
    plan_md = (tmp_path / "docs" / "projects" / "thing" / "plan.md").read_text()
    assert "- Depends on: T-03" in plan_md
    assert "- Depends on: T-01" not in plan_md


def test_task_rewire_rejects_bad_inputs_without_mutating(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "base", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01"])                       # T-01
    runner.invoke(app, ["task", "add", "--text", "dependent", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01", "--depends-on", "T-01"])  # T-02
    plan_md = tmp_path / "docs" / "projects" / "thing" / "plan.md"
    before = plan_md.read_bytes()
    r = runner.invoke(app, ["task", "rewire", "--from", "T-01", "--to", "T-99"])  # --to nonexistent
    assert r.exit_code != 0
    r = runner.invoke(app, ["task", "rewire", "--from", "T-01", "--to", "T-01"])  # --to == --from
    assert r.exit_code != 0
    r = runner.invoke(app, ["task", "rewire", "--from", "T-99", "--to", "T-01"])  # --from nonexistent
    assert r.exit_code != 0
    assert plan_md.read_bytes() == before  # rejected commands leave plan.md untouched


def test_task_add_supersede_offers_rewire_for_dependents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "base", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01"])                          # T-01
    runner.invoke(app, ["task", "add", "--text", "dep one", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01", "--depends-on", "T-01"])  # T-02
    runner.invoke(app, ["task", "add", "--text", "dep two", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01", "--depends-on", "T-01"])  # T-03
    r = runner.invoke(app, ["task", "add", "--text", "replacement", "--acceptance", "a",
                            "--verify", "v", "--from", "REQ-01", "--supersedes", "T-01"])  # T-04
    assert r.exit_code == 0
    out = r.output
    assert "T-02" in out and "T-03" in out
    assert "specflo task rewire --from T-01 --to T-04" in out
    # the detect-and-offer must NOT modify the dependents
    plan_md = (tmp_path / "docs" / "projects" / "thing" / "plan.md").read_text()
    assert plan_md.count("- Depends on: T-01") == 2


def test_task_add_supersede_prints_no_offer_when_no_dependents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "lonely", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01"])                       # T-01
    r = runner.invoke(app, ["task", "add", "--text", "replace", "--acceptance", "a",
                            "--verify", "v", "--from", "REQ-01", "--supersedes", "T-01"])  # T-02
    assert r.exit_code == 0
    assert "task rewire" not in r.output


def test_task_show_guides_when_blocked_by_superseded_dep(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "base", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01"])                          # T-01
    runner.invoke(app, ["task", "add", "--text", "dependent", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01", "--depends-on", "T-01"])  # T-02
    runner.invoke(app, ["task", "add", "--text", "replacement", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01", "--supersedes", "T-01"])  # T-03
    runner.invoke(app, ["task", "start", "T-03"])
    runner.invoke(app, ["task", "done", "T-03"])   # T-03 done -> nothing actionable, T-02 stuck
    r = runner.invoke(app, ["task", "show"])        # no id
    assert r.exit_code != 0
    out = r.output
    for token in ("T-02", "T-01", "T-03", "specflo task rewire --from T-01 --to T-03"):
        assert token in out


def test_status_and_checkpoint_guide_when_blocked_by_superseded_dep(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "base", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01"])                          # T-01
    runner.invoke(app, ["task", "add", "--text", "dependent", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01", "--depends-on", "T-01"])  # T-02
    runner.invoke(app, ["advance"])                 # plan -> execute (valid plan)
    runner.invoke(app, ["task", "add", "--text", "replacement", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01", "--supersedes", "T-01"])  # T-03
    runner.invoke(app, ["task", "start", "T-03"])
    runner.invoke(app, ["task", "done", "T-03"])    # nothing actionable; T-02 stuck on superseded T-01

    out = runner.invoke(app, ["status"]).output
    assert "T-02" in out
    assert "specflo task rewire --from T-01 --to T-03" in out

    cp = runner.invoke(app, ["checkpoint"]).output
    assert "T-02" in cp
    assert "specflo task rewire --from T-01 --to T-03" in cp


def test_status_shows_progress_line_at_plan_phase(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "build it",
                        "--acceptance", "it works", "--verify", "uv run pytest",
                        "--from", "REQ-01"])
    data = _json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["progress"]["total"] == 1
    out = runner.invoke(app, ["status"]).output
    assert "Tasks:" in out and "next: T-01" in out


def test_validate_execute_reports_reconcile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_execute(runner, app, tmp_path)
    r = runner.invoke(app, ["validate", "execute", "--json"])
    assert r.exit_code == 1                      # T-01 still pending
    assert "not all tasks are done" in r.output


def test_task_show_renders_brief_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_execute(runner, app, tmp_path)
    r = runner.invoke(app, ["task", "show", "--json"])
    assert r.exit_code == 0
    data = _json.loads(r.output)
    assert data["task"]["id"] == "T-01"
    assert data["requirements"][0]["id"] == "REQ-01"


def test_task_show_defaults_to_next_actionable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_execute(runner, app, tmp_path)
    r = runner.invoke(app, ["task", "show"])           # no id
    assert r.exit_code == 0
    assert "T-01" in r.output


def _execute_with_two_milestones(runner, app, tmp_path):
    """At execute with T-01 in current M-01 and a ready T-02 in later M-02."""
    _project_at_execute(runner, app, tmp_path)                       # T-01 pending
    runner.invoke(app, ["milestone", "add", "--text", "First", "--exit", "a"])   # M-01
    runner.invoke(app, ["milestone", "add", "--text", "Second", "--exit", "b"])  # M-02
    runner.invoke(app, ["task", "set-milestone", "T-01", "M-01"])
    runner.invoke(app, ["task", "add", "--text", "later work", "--acceptance", "ok",
                        "--verify", "true", "--from", "REQ-01", "--milestone", "M-02"])  # T-02


def test_task_show_steers_default_to_current_milestone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _execute_with_two_milestones(runner, app, tmp_path)   # T-01 (M-01) and T-02 (M-02) both ready
    r = runner.invoke(app, ["task", "show"])              # no id -> current milestone
    assert r.exit_code == 0
    assert "T-01" in r.output and "working ahead" not in r.output.lower()


def test_task_show_labels_working_ahead_in_text_and_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _execute_with_two_milestones(runner, app, tmp_path)
    runner.invoke(app, ["task", "start", "T-01"])     # current M-01 now has no ready pending task
    r = runner.invoke(app, ["task", "show"])          # no id -> steers ahead to T-02
    assert r.exit_code == 0
    assert "T-02" in r.output and "working ahead" in r.output.lower()
    rj = runner.invoke(app, ["task", "show", "--json"])
    data = _json.loads(rj.output)
    assert data["task"]["id"] == "T-02" and data["working_ahead"] is True


def test_status_shows_current_milestone_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _execute_with_two_milestones(runner, app, tmp_path)   # current M-01 holds T-01
    data = _json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["milestone"]["id"] == "M-01"
    assert data["milestone"]["done"] == 0 and data["milestone"]["total"] == 1
    out = runner.invoke(app, ["status"]).output
    assert "Milestone:" in out and "M-01" in out and "0/1" in out


def test_status_next_line_steers_to_current_milestone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_execute(runner, app, tmp_path)                         # creates T-01 (pending)
    runner.invoke(app, ["milestone", "add", "--text", "First", "--exit", "a"])    # M-01
    runner.invoke(app, ["milestone", "add", "--text", "Second", "--exit", "b"])   # M-02
    runner.invoke(app, ["task", "set-milestone", "T-01", "M-02"])      # T-01 -> later milestone
    runner.invoke(app, ["task", "add", "--text", "current work", "--acceptance", "ok",
                        "--verify", "true", "--from", "REQ-01", "--milestone", "M-01"])  # T-02 -> current
    out = runner.invoke(app, ["status"]).output
    # Current milestone is M-01 (holds T-02); the next line leads with T-02 — the
    # same task `task show` steers to — not document-order T-01 (REQ-13).
    assert "next: T-02" in out


def test_status_shows_no_milestone_line_for_a_milestone_free_plan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_execute(runner, app, tmp_path)            # no milestones
    data = _json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert "milestone" not in data                        # omitted when not meaningful
    assert "Milestone:" not in runner.invoke(app, ["status"]).output


# --- soft milestone-boundary verify beat surfaced in the CLI (T-09) ------


def _execute_at_boundary(runner, app, tmp_path):
    """At execute: M-01 (T-01) done with a distinctive Exit item, M-02 (T-02)
    pending — sitting exactly at the M-01 -> M-02 milestone boundary."""
    _project_at_execute(runner, app, tmp_path)                       # T-01 pending
    runner.invoke(app, ["milestone", "add", "--text", "First",
                        "--exit", "login flow ships"])               # M-01
    runner.invoke(app, ["milestone", "add", "--text", "Second", "--exit", "b"])  # M-02
    runner.invoke(app, ["task", "set-milestone", "T-01", "M-01"])
    runner.invoke(app, ["task", "add", "--text", "later work", "--acceptance", "ok",
                        "--verify", "true", "--from", "REQ-01", "--milestone", "M-02"])  # T-02
    runner.invoke(app, ["task", "start", "T-01"])
    runner.invoke(app, ["task", "done", "T-01"])


def test_status_surfaces_the_milestone_boundary_beat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _execute_at_boundary(runner, app, tmp_path)
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0                                  # soft beat never blocks
    assert "M-01" in r.output and "login flow ships" in r.output
    assert "proceed" in r.output.lower()
    data = _json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["boundary"]["id"] == "M-01" and data["boundary"]["all_complete"] is False


def test_task_show_surfaces_the_milestone_boundary_beat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _execute_at_boundary(runner, app, tmp_path)
    r = runner.invoke(app, ["task", "show"])                # default -> T-02, plus the M-01 beat
    assert r.exit_code == 0
    assert "T-02" in r.output                               # still shows the next task
    assert "login flow ships" in r.output and "proceed" in r.output.lower()
    data = _json.loads(runner.invoke(app, ["task", "show", "--json"]).output)
    assert data["boundary"]["id"] == "M-01"


def test_task_show_surfaces_all_complete_boundary_instead_of_erroring(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _execute_at_boundary(runner, app, tmp_path)
    runner.invoke(app, ["task", "start", "T-02"])
    runner.invoke(app, ["task", "done", "T-02"])            # every task now done
    r = runner.invoke(app, ["task", "show"])                # nothing actionable -> all-complete beat
    assert r.exit_code == 0                                 # soft beat, never a non-zero stop
    assert "M-02" in r.output
    assert "proceed" in r.output.lower() and "advance" in r.output.lower()
    data = _json.loads(runner.invoke(app, ["task", "show", "--json"]).output)
    assert data["task"] is None and data["boundary"]["all_complete"] is True


def test_checkpoint_surfaces_the_milestone_boundary_beat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _execute_at_boundary(runner, app, tmp_path)
    r = runner.invoke(app, ["checkpoint"])
    assert r.exit_code == 0
    assert "login flow ships" in r.output and "proceed" in r.output.lower()


def test_no_boundary_beat_off_a_boundary_and_none_for_milestone_free(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_execute(runner, app, tmp_path)              # no milestones
    assert "proceed" not in runner.invoke(app, ["status"]).output.lower()
    data = _json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert "boundary" not in data                           # omitted when not meaningful


def test_boundary_beat_has_no_milestone_gate_verb_in_the_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    # The boundary is a soft, derived beat — there is deliberately no verb that
    # gates or marks a milestone verified/done/complete (REQ-14).
    for verb in ("verify", "gate", "done", "complete", "pass", "advance"):
        r = runner.invoke(app, ["milestone", verb])
        assert r.exit_code != 0, f"unexpected `milestone {verb}` command exists"


def test_advance_completes_project_at_execute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_execute(runner, app, tmp_path)
    # T-01 pending -> advance refuses, nothing mutated
    assert runner.invoke(app, ["advance"]).exit_code == 1
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    assert "status: active" in proj_md.read_text()
    # do the task, then advance completes the project
    runner.invoke(app, ["task", "start", "T-01"])
    runner.invoke(app, ["task", "done", "T-01"])
    r = runner.invoke(app, ["advance", "--json"])
    assert r.exit_code == 0
    data = _json.loads(r.output)
    assert data["complete"] is True and data["to"] is None
    assert "status: complete" in proj_md.read_text()
    # idempotent: a second advance reports already-complete, mutates nothing
    r2 = runner.invoke(app, ["advance", "--json"])
    assert r2.exit_code == 0
    assert _json.loads(r2.output)["complete"] is True


def test_status_complete_project_is_progress_aware(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_execute(runner, app, tmp_path)
    runner.invoke(app, ["task", "start", "T-01"])
    runner.invoke(app, ["task", "done", "T-01"])
    runner.invoke(app, ["advance"])                       # completes the project
    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["status"] == "complete"
    assert "complete" in data["next_step"].lower()
    assert "complete" in runner.invoke(app, ["status"]).output.lower()


def test_execute_loop_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_execute(runner, app, tmp_path)            # phase: execute, T-01 pending
    assert "T-01" in runner.invoke(app, ["task", "show"]).output
    assert runner.invoke(app, ["task", "start", "T-01"]).exit_code == 0
    assert runner.invoke(app, ["task", "done", "T-01"]).exit_code == 0
    assert runner.invoke(app, ["validate", "execute"]).exit_code == 0
    r = runner.invoke(app, ["advance", "--json"])
    assert _json.loads(r.output)["complete"] is True
    # the guide's Skills line itself must name execute (not merely the pipeline line)
    skills_line = runner.invoke(app, ["guide"]).output.split("Skills:", 1)[1].splitlines()[0]
    assert "execute" in skills_line


# --- shelve (T-02) -------------------------------------------------------


def _active_project_at_spec(cwd):
    """init + new + advance to the spec phase; returns the project.md path."""
    _ready_brainstorm(cwd)
    runner.invoke(app, ["advance"])  # brainstorm -> spec
    return cwd / "docs" / "projects" / "my-thing" / "project.md"


def test_shelve_sets_status_shelved_and_keeps_phase(cwd):
    project_md = _active_project_at_spec(cwd)
    result = runner.invoke(app, ["shelve"])
    assert result.exit_code == 0
    text = project_md.read_text()
    assert "status: shelved" in text
    assert "phase: spec" in text  # phase preserved


def test_shelve_keeps_the_active_project_pointer(cwd):
    _active_project_at_spec(cwd)
    runner.invoke(app, ["shelve"])
    assert config.load_config(cwd).active_project == "my-thing"


def test_shelve_stores_reason_in_frontmatter(cwd):
    project_md = _active_project_at_spec(cwd)
    result = runner.invoke(app, ["shelve", "--reason", "not worth it"])
    assert result.exit_code == 0
    assert "shelved_reason: not worth it" in project_md.read_text()


def test_shelve_without_reason_leaves_reason_absent(cwd):
    project_md = _active_project_at_spec(cwd)
    runner.invoke(app, ["shelve"])
    assert "shelved_reason" not in project_md.read_text()


def test_shelve_refreshes_the_checkpoint(cwd):
    _active_project_at_spec(cwd)
    runner.invoke(app, ["shelve", "--reason", "later"])
    cp = cwd / "docs" / "projects" / "my-thing" / "checkpoint.md"
    assert cp.is_file()


def test_shelve_json_emits_slug_status_and_reason(cwd):
    _active_project_at_spec(cwd)
    data = json.loads(
        runner.invoke(app, ["shelve", "--reason", "nope", "--json"]).output
    )
    assert data["slug"] == "my-thing"
    assert data["status"] == "shelved"
    assert data["reason"] == "nope"


def test_shelve_targets_a_named_non_active_project(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo"])  # Bravo is now active
    result = runner.invoke(app, ["shelve", "Alpha"])
    assert result.exit_code == 0
    alpha_md = cwd / "docs" / "projects" / "alpha" / "project.md"
    assert "status: shelved" in alpha_md.read_text()
    assert config.load_config(cwd).active_project == "bravo"  # pointer untouched


def test_shelve_reshelve_updates_the_reason(cwd):
    project_md = _active_project_at_spec(cwd)
    runner.invoke(app, ["shelve", "--reason", "first"])
    runner.invoke(app, ["shelve", "--reason", "second"])
    text = project_md.read_text()
    assert "status: shelved" in text
    assert "shelved_reason: second" in text
    assert "first" not in text


def test_shelve_without_active_project_fails(cwd):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["shelve"])
    assert result.exit_code != 0


def test_shelve_refuses_a_complete_project(cwd):
    from specflo import projects
    _active_project_at_spec(cwd)
    projects.complete_project(cwd, config.load_config(cwd), "my-thing")  # terminal
    result = runner.invoke(app, ["shelve"])
    assert result.exit_code != 0
    assert "complete" in result.output.lower()  # message names the terminal state
    project_md = cwd / "docs" / "projects" / "my-thing" / "project.md"
    assert "status: complete" in project_md.read_text()  # status unchanged


# --- resume (T-04) -------------------------------------------------------


def test_resume_unshelves_and_reactivates(cwd):
    project_md = _active_project_at_spec(cwd)
    runner.invoke(app, ["shelve", "--reason", "later"])
    result = runner.invoke(app, ["resume", "my-thing"])
    assert result.exit_code == 0
    text = project_md.read_text()
    assert "status: active" in text
    assert "shelved_reason" not in text  # reason cleared
    assert "phase: spec" in text  # phase unchanged
    assert config.load_config(cwd).active_project == "my-thing"


def test_resume_bare_resumes_the_active_shelved_project(cwd):
    project_md = _active_project_at_spec(cwd)
    runner.invoke(app, ["shelve"])  # my-thing stays the active pointer
    result = runner.invoke(app, ["resume"])
    assert result.exit_code == 0
    assert "status: active" in project_md.read_text()


def test_resume_sets_active_pointer_to_a_named_project(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["shelve"])        # Alpha shelved, still the active pointer
    runner.invoke(app, ["new", "Bravo"])  # Bravo now active
    result = runner.invoke(app, ["resume", "Alpha"])
    assert result.exit_code == 0
    assert config.load_config(cwd).active_project == "alpha"
    alpha_md = cwd / "docs" / "projects" / "alpha" / "project.md"
    assert "status: active" in alpha_md.read_text()


def test_resume_refreshes_the_checkpoint(cwd):
    _active_project_at_spec(cwd)
    runner.invoke(app, ["shelve"])
    runner.invoke(app, ["resume"])
    cp = cwd / "docs" / "projects" / "my-thing" / "checkpoint.md"
    assert cp.is_file()


def test_resume_json_emits_slug_and_status(cwd):
    _active_project_at_spec(cwd)
    runner.invoke(app, ["shelve"])
    data = json.loads(runner.invoke(app, ["resume", "--json"]).output)
    assert data["slug"] == "my-thing"
    assert data["status"] == "active"


def test_resume_refuses_a_not_shelved_project(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])  # active, not shelved
    runner.invoke(app, ["new", "Bravo"])  # Bravo now the active pointer
    result = runner.invoke(app, ["resume", "Alpha"])  # Alpha is active-status, not shelved
    assert result.exit_code != 0
    assert "not shelved" in result.output.lower()
    alpha_md = cwd / "docs" / "projects" / "alpha" / "project.md"
    assert "status: active" in alpha_md.read_text()  # status unchanged
    assert config.load_config(cwd).active_project == "bravo"  # pointer not stolen


def test_advance_refuses_a_shelved_project(cwd):
    _ready_spec(cwd)
    runner.invoke(app, ["advance"])  # spec -> plan
    runner.invoke(app, ["shelve", "--reason", "paused"])
    project_md = cwd / "docs" / "projects" / "my-thing" / "project.md"
    result = runner.invoke(app, ["advance"])
    assert result.exit_code != 0
    assert "resume" in result.output.lower()  # guidance to resume first
    text = project_md.read_text()
    assert "phase: plan" in text      # phase unchanged
    assert "status: shelved" in text  # status unchanged


def test_switch_onto_a_shelved_project_keeps_it_shelved(cwd):
    # Regression lock: switch moves the pointer but must not un-shelve. Only
    # 'resume' un-shelves; switching to a shelved project leaves it shelved.
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["shelve", "--reason", "later"])  # Alpha shelved (still active pointer)
    runner.invoke(app, ["new", "Bravo"])                 # Bravo now active
    result = runner.invoke(app, ["switch", "Alpha"])
    assert result.exit_code == 0
    assert config.load_config(cwd).active_project == "alpha"  # pointer moved
    alpha_md = cwd / "docs" / "projects" / "alpha" / "project.md"
    assert "status: shelved" in alpha_md.read_text()  # status unchanged by switch


# --- status surfaces shelved state (T-09) --------------------------------


def test_status_marks_a_shelved_project_and_shows_reason(cwd):
    _active_project_at_spec(cwd)
    runner.invoke(app, ["shelve", "--reason", "waiting on api"])
    out = runner.invoke(app, ["status"]).output
    assert "(shelved" in out         # phase line marked shelved (mirrors '(complete)')
    assert "waiting on api" in out   # reason shown when set
    low = out.lower()
    assert "resume" in low           # Next line directs to resume-or-new
    assert "new" in low


def test_status_shelved_without_reason_is_marked_without_reason_text(cwd):
    _active_project_at_spec(cwd)
    runner.invoke(app, ["shelve"])
    out = runner.invoke(app, ["status"]).output
    assert "(shelved)" in out        # marked, no trailing reason


def test_status_json_reports_shelved_status_and_reason(cwd):
    _active_project_at_spec(cwd)
    runner.invoke(app, ["shelve", "--reason", "later"])
    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["status"] == "shelved"
    assert data["shelved_reason"] == "later"


def test_status_json_omits_shelved_reason_for_a_non_shelved_project(cwd):
    _active_project_at_spec(cwd)  # active, not shelved
    data = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert data["status"] == "active"
    assert "shelved_reason" not in data  # no empty field on non-shelved payloads


# --- list marks shelved projects (T-10) ----------------------------------


def test_list_marks_a_shelved_project_with_reason(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["shelve", "--reason", "on hold"])
    out = runner.invoke(app, ["list"]).output
    assert "shelved" in out.lower()   # shelved marker present
    assert "on hold" in out           # reason appended when set
    assert "✓ complete" not in out    # distinct from the complete marker


def test_list_shelved_marker_is_distinct_from_complete(cwd):
    from specflo import projects
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["shelve"])     # Alpha shelved
    runner.invoke(app, ["new", "Bravo"])
    projects.complete_project(cwd, config.load_config(cwd), "bravo")
    out = runner.invoke(app, ["list"]).output
    alpha_line = next(l for l in out.splitlines() if "alpha" in l)
    bravo_line = next(l for l in out.splitlines() if "bravo" in l)
    assert "shelved" in alpha_line.lower()
    assert "complete" not in alpha_line.lower()   # shelved entry isn't marked complete
    assert "complete" in bravo_line.lower()
    assert "shelved" not in bravo_line.lower()    # ...and vice versa


def test_list_json_reports_shelved_status(cwd):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["shelve", "--reason", "later"])
    data = json.loads(runner.invoke(app, ["list", "--json"]).output)
    alpha = next(p for p in data["projects"] if p["slug"] == "alpha")
    assert alpha["status"] == "shelved"


def test_hook_directives_are_ascii():
    """Guard: session-start directive strings stay ASCII (no em-dashes/arrows)."""
    from specflo import hook

    for name in dir(hook):
        if name.isupper() and isinstance(getattr(hook, name), str):
            getattr(hook, name).encode("ascii")  # raises on any non-ASCII


def test_cli_output_stays_ascii(tmp_path, monkeypatch):
    """Guard: user-facing command output stays ASCII across the pipeline.

    Locks the em-dash/arrow/middle-dot -> ASCII cleanup so the glyphs can't
    creep back into terminal output.
    """
    monkeypatch.chdir(tmp_path)
    proj = tmp_path / "docs" / "projects" / "my-thing"
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    # valid brainstorm -> advance to spec
    runner.invoke(app, ["decision", "add", "--text", "Use SQLite"])
    bs = proj / "brainstorm.md"
    bs.write_text(
        bs.read_text().replace(
            "## Out of scope / Deferred\n"
            "<!-- required, must be non-empty before validate passes -->",
            "## Out of scope / Deferred\n- nothing else.",
        )
    )
    runner.invoke(app, ["advance"])
    # valid spec -> advance to plan
    runner.invoke(app, ["spec", "start"])
    runner.invoke(
        app,
        ["requirement", "add", "--text", "store data",
         "--acceptance", "survives restart", "--from", "D-01"],
    )
    sp = proj / "spec.md"
    sp.write_text(
        sp.read_text()
        .replace("### In scope\n<!-- required, non-empty -->", "### In scope\n- it.")
        .replace(
            "### Out of scope\n"
            "<!-- required, non-empty; carried from the brainstorm's Out of scope / Deferred -->",
            "### Out of scope\n- not it.",
        )
    )
    runner.invoke(app, ["advance"])
    # plan + task -> advance to execute
    runner.invoke(app, ["plan", "start"])
    runner.invoke(
        app,
        ["task", "add", "--text", "build it", "--acceptance", "works",
         "--verify", "uv run pytest", "--from", "REQ-01"],
    )
    runner.invoke(app, ["advance"])
    runner.invoke(app, ["task", "start", "T-01"])

    for cmd in (
        ["status"], ["status", "--json"], ["checkpoint"], ["list"], ["guide"],
        ["task", "list"], ["task", "show"], ["validate", "execute"], ["hook", "print"],
    ):
        runner.invoke(app, cmd).output.encode("ascii")  # raises on any non-ASCII


# --- Milestones (T-01) --------------------------------------------------------


def test_milestone_add_cli_appends_entry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    r = runner.invoke(app, ["milestone", "add", "--text", "Auth works",
                            "--exit", "login", "--exit", "logout", "--json"])
    assert r.exit_code == 0
    data = _json.loads(r.output)
    assert data["id"] == "M-01"
    assert data["exit"] == ["login", "logout"]
    plan_md = (tmp_path / "docs" / "projects" / "thing" / "plan.md").read_text()
    assert "### M-01 — Auth works" in plan_md
    assert "  - login" in plan_md and "  - logout" in plan_md


def test_milestone_add_cli_requires_an_exit_item(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    r = runner.invoke(app, ["milestone", "add", "--text", "No exit"])
    assert r.exit_code != 0  # --exit is required (>=1)


def test_milestone_command_appears_in_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "milestone" in result.output


# --- milestone list (T-02) ----------------------------------------------------


def _set_task_milestone(tmp_path, tid, mid):
    """Directly stamp a `- Milestone:` field onto a task in plan.md (CLI assignment
    lands in T-03; this crafts membership for list/rollup tests)."""
    from specflo import markdown
    plan_md = tmp_path / "docs" / "projects" / "thing" / "plan.md"
    plan_md.write_text(markdown.set_entry_field(plan_md.read_text(), tid, "Milestone", mid))


def test_milestone_list_cli_reports_rollup_and_current(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["task", "add", "--text", "one", "--acceptance", "a",
                        "--verify", "true", "--from", "REQ-01"])                 # T-01
    runner.invoke(app, ["task", "add", "--text", "two", "--acceptance", "a",
                        "--verify", "true", "--from", "REQ-01"])                 # T-02
    runner.invoke(app, ["milestone", "add", "--text", "First", "--exit", "ships"])  # M-01
    _set_task_milestone(tmp_path, "T-01", "M-01")
    _set_task_milestone(tmp_path, "T-02", "M-01")

    data = _json.loads(runner.invoke(app, ["milestone", "list", "--json"]).output)
    assert data["current"] == "M-01"
    assert data["milestones"][0]["id"] == "M-01"
    assert data["milestones"][0]["done"] == 0 and data["milestones"][0]["total"] == 2

    out = runner.invoke(app, ["milestone", "list"]).output
    assert "M-01" in out and "0/2" in out and "First" in out

    # completing both member tasks flips the milestone complete; current -> none
    for tid in ("T-01", "T-02"):
        runner.invoke(app, ["task", "start", tid]); runner.invoke(app, ["task", "done", tid])
    data = _json.loads(runner.invoke(app, ["milestone", "list", "--json"]).output)
    assert data["milestones"][0]["complete"] is True
    assert data["current"] is None


def test_milestone_list_cli_is_friendly_when_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    r = runner.invoke(app, ["milestone", "list"])
    assert r.exit_code == 0
    assert "no milestones" in r.output.lower()
    data = _json.loads(runner.invoke(app, ["milestone", "list", "--json"]).output)
    assert data["milestones"] == [] and data["current"] is None


# --- Task milestone assignment (T-03) -----------------------------------------


def test_task_add_milestone_cli_writes_field(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["milestone", "add", "--text", "First", "--exit", "ships"])  # M-01
    r = runner.invoke(app, ["task", "add", "--text", "member", "--acceptance", "a",
                            "--verify", "v", "--from", "REQ-01", "--milestone", "M-01", "--json"])
    assert r.exit_code == 0
    plan_md = (tmp_path / "docs" / "projects" / "thing" / "plan.md").read_text()
    assert "- Milestone: M-01" in plan_md


def test_task_add_milestone_cli_rejects_unknown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    r = runner.invoke(app, ["task", "add", "--text", "x", "--acceptance", "a",
                            "--verify", "v", "--from", "REQ-01", "--milestone", "M-09"])
    assert r.exit_code != 0  # M-09 does not exist


def test_task_set_milestone_cli_reassigns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["milestone", "add", "--text", "First", "--exit", "a"])   # M-01
    runner.invoke(app, ["milestone", "add", "--text", "Second", "--exit", "b"])  # M-02
    runner.invoke(app, ["task", "add", "--text", "t", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01", "--milestone", "M-01"])  # T-01
    r = runner.invoke(app, ["task", "set-milestone", "T-01", "M-02", "--json"])
    assert r.exit_code == 0
    data = _json.loads(r.output)
    assert data["id"] == "T-01" and data["milestone"] == "M-02"
    plan_md = (tmp_path / "docs" / "projects" / "thing" / "plan.md").read_text()
    assert "- Milestone: M-02" in plan_md and "- Milestone: M-01" not in plan_md


# --- milestone show (T-04) ----------------------------------------------------


def test_milestone_show_cli_renders_detail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    runner.invoke(app, ["milestone", "add", "--text", "First",
                        "--exit", "login works", "--exit", "logout works"])   # M-01
    runner.invoke(app, ["task", "add", "--text", "build login", "--acceptance", "a",
                        "--verify", "v", "--from", "REQ-01", "--milestone", "M-01"])  # T-01
    data = _json.loads(runner.invoke(app, ["milestone", "show", "M-01", "--json"]).output)
    assert data["id"] == "M-01" and data["title"] == "First"
    assert data["exit_items"] == ["login works", "logout works"]
    assert [m["id"] for m in data["members"]] == ["T-01"]
    assert data["reqs"] == ["REQ-01"]
    assert data["total"] == 1 and data["complete"] is False

    out = runner.invoke(app, ["milestone", "show", "M-01"]).output
    assert "M-01" in out and "First" in out
    assert "login works" in out and "logout works" in out
    assert "T-01" in out and "REQ-01" in out


def test_milestone_show_cli_unknown_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_plan_phase(runner, app, tmp_path)
    r = runner.invoke(app, ["milestone", "show", "M-09"])
    assert r.exit_code != 0


# --- Backward-compatibility dormancy guard across CLI surfaces (T-10) --------


def test_milestone_free_plan_text_surfaces_stay_silent_about_milestones(tmp_path, monkeypatch):
    # REQ-04: with zero milestones, no human-facing surface says a word about
    # milestones — validate/show/list/status/checkpoint match pre-feature output.
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_execute(runner, app, tmp_path)              # T-01 pending, no milestones
    words = ("milestone", "working ahead", "exit checklist", "proceed")
    for argv in (["validate", "plan"], ["task", "show"], ["task", "list"],
                 ["status"], ["checkpoint"]):
        r = runner.invoke(app, argv)
        assert r.exit_code == 0, f"{argv} -> {r.exit_code}: {r.output}"
        low = r.output.lower()
        assert not any(w in low for w in words), f"{argv} leaked milestone output: {r.output}"


def test_milestone_free_plan_json_surfaces_carry_dormant_contract(tmp_path, monkeypatch):
    # The machine contract stays stable and dormant: status omits the milestone /
    # boundary fields entirely; task show reports working_ahead False, boundary None.
    monkeypatch.chdir(tmp_path)
    from specflo.cli import app
    _project_at_execute(runner, app, tmp_path)
    status = _json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert "milestone" not in status and "boundary" not in status
    brief = _json.loads(runner.invoke(app, ["task", "show", "--json"]).output)
    assert brief["working_ahead"] is False and brief["boundary"] is None
