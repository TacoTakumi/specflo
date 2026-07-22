"""Derived read-path doneness in `specflo status` (T-04).

``build_status`` runs the current phase's real validator inline for
brainstorm/spec/plan (REQ-01/03): a validating artifact reads as offer-advance,
a failing one as work-in-progress, recomputed every read and mutating nothing.
Execute keeps its progress-based hint (REQ-05).
"""

from typer.testing import CliRunner

from specflo import config, projects, spec, status, workflow
from specflo.cli import app

runner = CliRunner()


def _validating_spec_project(tmp_path):
    """A spec-phase, active 'Thing' whose spec.md passes validate_spec.

    Returns ``(cfg, project, spec_md_path)``. The project is switched active so
    the ``specflo status`` command resolves it.
    """
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-07-06")
    projects.switch_project(tmp_path, cfg, "Thing")  # active + persisted
    spec.start_spec(tmp_path, cfg, "thing", today="2026-07-06")
    spec.add_requirement(tmp_path, cfg, "thing", "a req", acceptance="it passes",
                         today="2026-07-06")
    spec_md = tmp_path / "docs" / "projects" / "thing" / "spec.md"
    spec_md.write_text(
        spec_md.read_text()
        .replace("### In scope\n<!-- required, non-empty -->",
                 "### In scope\n- the CLI.")
        .replace("### Out of scope\n"
                 "<!-- required, non-empty; carried from the brainstorm's "
                 "Out of scope / Deferred -->",
                 "### Out of scope\n- the GUI.")
    )
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    proj_md.write_text(proj_md.read_text().replace("phase: brainstorm", "phase: spec"))
    return cfg, projects.load_project(tmp_path, cfg, "thing"), spec_md


def test_build_status_spec_that_validates_offers_advance(tmp_path):
    cfg, project, _spec_md = _validating_spec_project(tmp_path)
    assert spec.validate_spec(tmp_path, cfg, "thing") == []       # precondition
    info = status.build_status(tmp_path, cfg, project)
    assert "specflo advance" in info["next_step"]                 # offers the move
    assert "plan" in info["next_step"]                            # names the next phase


def test_build_status_derives_doneness_on_every_read(tmp_path):
    # REQ-02/03: breaking validation reverts the very next status to the work hint
    # with no intervening command.
    cfg, project, spec_md = _validating_spec_project(tmp_path)
    assert "specflo advance" in status.build_status(tmp_path, cfg, project)["next_step"]
    spec_md.write_text(spec_md.read_text().replace("### In scope\n- the CLI.",
                                                   "### In scope\n"))
    reverted = status.build_status(tmp_path, cfg, project)["next_step"]
    assert reverted == workflow.next_step("spec")                 # back to the work hint
    assert "specflo advance" not in reverted


def test_build_status_mutates_nothing(tmp_path):
    # REQ-04: deriving doneness leaves the phase and status untouched.
    cfg, project, _spec_md = _validating_spec_project(tmp_path)
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    before, phase, st = proj_md.read_text(), project.phase, project.status
    status.build_status(tmp_path, cfg, project)
    assert proj_md.read_text() == before
    assert (project.phase, project.status) == (phase, st)


def test_status_command_on_validating_spec_exits_zero_and_offers_advance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _validating_spec_project(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0                                  # still exits 0 (REQ-01)
    assert "specflo advance" in result.output
    assert "plan" in result.output


def _plan_at_execute(tmp_path):
    """A 'Thing' at execute with a single pending task T-01 (progress-based hint)."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-07-06")
    projects.switch_project(tmp_path, cfg, "Thing")
    spec.start_spec(tmp_path, cfg, "thing", today="2026-07-06")
    spec.add_requirement(tmp_path, cfg, "thing", "r", acceptance="a", today="2026-07-06")
    from specflo import plan
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    proj_md.write_text(proj_md.read_text().replace("phase: brainstorm", "phase: execute"))
    plan.start_plan(tmp_path, cfg, "thing", today="2026-07-06")
    plan.add_task(tmp_path, cfg, "thing", "build it", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-07-06")       # T-01 pending
    return cfg, projects.load_project(tmp_path, cfg, "thing")


def test_build_status_execute_phase_progress_based_and_untouched(tmp_path):
    # REQ-05: execute keeps its progress-based hint; the validator branch never
    # fires there.
    cfg, project = _plan_at_execute(tmp_path)
    info = status.build_status(tmp_path, cfg, project)
    assert "T-01" in info["next_step"]                            # names the next task
    assert "task show" in info["next_step"]
    assert "specflo advance" not in info["next_step"]


# --- derived doneness at the other two read-path phases (brainstorm, plan) ---
# The spec phase is covered above; these lock the shared VALIDATORS.get(phase)
# path at brainstorm and plan too.


def _validating_brainstorm_project(tmp_path):
    """A brainstorm-phase, active 'Thing' whose brainstorm.md validates."""
    from specflo import brainstorm
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-07-06")
    projects.switch_project(tmp_path, cfg, "Thing")
    brainstorm.start_brainstorm(tmp_path, cfg, "thing", today="2026-07-06")
    brainstorm.add_decision(tmp_path, cfg, "thing", "use SQLite", today="2026-07-06")
    bs = tmp_path / "docs" / "projects" / "thing" / "brainstorm.md"
    bs.write_text(bs.read_text().replace(
        "## Out of scope / Deferred\n"
        "<!-- required, must be non-empty before validate passes -->",
        "## Out of scope / Deferred\n- the GUI."))
    return cfg, projects.load_project(tmp_path, cfg, "thing")


def _validating_plan_project(tmp_path):
    """A plan-phase, active 'Thing' whose plan.md validates (1 req, 1 task)."""
    from specflo import plan
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-07-06")
    projects.switch_project(tmp_path, cfg, "Thing")
    spec.start_spec(tmp_path, cfg, "thing", today="2026-07-06")
    spec.add_requirement(tmp_path, cfg, "thing", "r", acceptance="a", today="2026-07-06")
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    proj_md.write_text(proj_md.read_text().replace("phase: brainstorm", "phase: plan"))
    plan.start_plan(tmp_path, cfg, "thing", today="2026-07-06")
    plan.add_task(tmp_path, cfg, "thing", "build it", acceptance="a passes",
                  verify="uv run pytest", implements=["REQ-01"], today="2026-07-06")
    return cfg, projects.load_project(tmp_path, cfg, "thing")


def test_build_status_brainstorm_that_validates_offers_advance(tmp_path):
    from specflo import brainstorm
    cfg, project = _validating_brainstorm_project(tmp_path)
    assert brainstorm.validate_brainstorm(tmp_path, cfg, "thing") == []  # precondition
    info = status.build_status(tmp_path, cfg, project)
    assert "specflo advance" in info["next_step"]                        # offers the move
    assert "spec" in info["next_step"]                                   # names next phase


def test_build_status_plan_that_validates_offers_advance(tmp_path):
    from specflo import plan
    cfg, project = _validating_plan_project(tmp_path)
    assert plan.validate_plan(tmp_path, cfg, "thing") == []              # precondition
    info = status.build_status(tmp_path, cfg, project)
    assert "specflo advance" in info["next_step"]                        # offers the move
    assert "execute" in info["next_step"]                               # names next phase


# --- the pi-extension arming threshold on the status payload (pi-extension T-04) ---
# The extension reads the threshold from `status --json` rather than opening
# .specflo/config.yaml (pi-extension REQ-28), so build_status carries the
# resolved value and the human render stays untouched.


def test_build_status_reports_the_default_arming_threshold(tmp_path):
    cfg, project, _spec_md = _validating_spec_project(tmp_path)
    info = status.build_status(tmp_path, cfg, project)
    assert info["context_threshold_percent"] == config.DEFAULT_CONTEXT_THRESHOLD_PERCENT
    assert isinstance(info["context_threshold_percent"], int)


def test_build_status_reports_a_configured_arming_threshold(tmp_path):
    cfg, project, _spec_md = _validating_spec_project(tmp_path)
    cfg.context_threshold_percent = 60
    config.save_config(tmp_path, cfg)
    reloaded = config.load_config(tmp_path)
    info = status.build_status(tmp_path, reloaded, project)
    assert info["context_threshold_percent"] == 60


def test_render_status_ignores_the_arming_threshold(tmp_path):
    # Machine-only field: the human block is byte-identical whatever it holds.
    cfg, project, _spec_md = _validating_spec_project(tmp_path)
    default = status.render_status(tmp_path, status.build_status(tmp_path, cfg, project))
    cfg.context_threshold_percent = 42
    tuned = status.render_status(tmp_path, status.build_status(tmp_path, cfg, project))
    assert tuned == default
    assert "42" not in tuned
