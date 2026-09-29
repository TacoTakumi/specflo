"""Derived read-path doneness in `specflo status` (T-04).

``build_status`` runs the current phase's real validator inline for
brainstorm/spec/plan (REQ-01/03): a validating artifact reads as offer-advance,
a failing one as work-in-progress, recomputed every read and mutating nothing.
Execute keeps its progress-based hint (REQ-05).
"""

import json

import pytest

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


def test_status_json_survives_an_invalid_config(tmp_path, monkeypatch):
    # The pi extension polls `status --json` every turn (REQ-25): a hand-edited
    # config that fails validation must not take the poll down. It degrades to
    # the shipped defaults, warns on stderr, and leaves stdout parseable JSON.
    _validating_spec_project(tmp_path)
    path = config.config_path(tmp_path)
    bad = {"autonomy": "bogus", "auto_max_passes": 0, "context_threshold_percent": 101}
    path.write_text(path.read_text() + "".join(f"{k}: {v!r}\n" for k, v in bad.items()))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["status", "--json"])

    assert result.exit_code == 0
    # `result.stdout`, not `result.output`: the latter interleaves stderr, which
    # is the whole point here - the warnings must not land in the JSON.
    data = json.loads(result.stdout)
    assert data["context_threshold_percent"] == (
        config.DEFAULT_CONTEXT_THRESHOLD_PERCENT
    )
    lines = result.stderr.strip().splitlines()
    assert len(lines) == len(bad)
    for key in bad:
        assert any(key in line for line in lines)
    assert "bogus" not in result.stdout  # warnings never reach stdout


# --- the auto-run block on the status payload (pi-extension T-06) -------------
# The extension asks the CLI whether an auto run is under way instead of reading
# the run-state file itself (pi-extension REQ-12/REQ-13).


def test_build_status_reports_no_auto_run_for_a_project_that_never_ran_auto(tmp_path):
    cfg, project = _plan_at_execute(tmp_path)
    info = status.build_status(tmp_path, cfg, project)
    assert info["auto_run"]["under_way"] is False


def test_build_status_reports_an_auto_run_under_way_after_a_pass(tmp_path):
    from specflo import auto
    cfg, project = _plan_at_execute(tmp_path)
    auto.auto_pass(tmp_path, max_passes=1000)                     # a continuable pass
    info = status.build_status(tmp_path, cfg, project)
    assert info["auto_run"]["under_way"] is True


def test_build_status_reports_no_auto_run_once_the_kill_switch_is_set(tmp_path):
    from specflo import auto
    cfg, project = _plan_at_execute(tmp_path)
    auto.auto_pass(tmp_path, max_passes=1000)
    auto.set_kill_switch(tmp_path, killed=True)
    info = status.build_status(tmp_path, cfg, project)
    assert info["auto_run"]["under_way"] is False


def test_build_status_reports_no_auto_run_after_a_terminal_stop(tmp_path):
    from specflo import auto
    cfg, project = _plan_at_execute(tmp_path)
    auto.auto_pass(tmp_path, max_passes=2)                        # continuable
    assert status.build_status(tmp_path, cfg, project)["auto_run"]["under_way"] is True
    auto.auto_pass(tmp_path, max_passes=2)                        # reaches the cap
    info = status.build_status(tmp_path, cfg, project)
    assert info["auto_run"]["under_way"] is False


def test_build_status_reports_no_auto_run_for_a_complete_project(tmp_path):
    from specflo import auto
    cfg, project = _plan_at_execute(tmp_path)
    auto.auto_pass(tmp_path, max_passes=1000)
    projects.complete_project(tmp_path, cfg, "thing")
    completed = projects.load_project(tmp_path, cfg, "thing")
    info = status.build_status(tmp_path, cfg, completed)
    assert info["auto_run"]["under_way"] is False


def test_render_status_ignores_the_auto_run_block(tmp_path):
    from specflo import auto
    cfg, project = _plan_at_execute(tmp_path)
    idle = status.render_status(tmp_path, status.build_status(tmp_path, cfg, project))
    auto.auto_pass(tmp_path, max_passes=1000)
    running = status.render_status(tmp_path, status.build_status(tmp_path, cfg, project))
    assert running == idle


# --- the review line (review-rounds REQ-11, REQ-12, REQ-09, REQ-19) -----------
# Status is the read surface for review rounds: how many, and where the latest
# one stands. Every fact on the line is derived from the round files themselves.


def _close_review(tmp_path, cfg, verdict, reason=None):
    import reviewhelp
    from specflo import review
    review.start_round(tmp_path, cfg, "thing", today="2026-08-01")
    return reviewhelp.close_round(
        tmp_path, cfg, "thing", verdict, reason=reason, today="2026-08-02"
    )


def test_status_review_line_names_the_count_and_the_latest_verdict(tmp_path):
    cfg, project = _plan_at_execute(tmp_path)
    _close_review(tmp_path, cfg, "changes-requested")
    _close_review(tmp_path, cfg, "ready-to-merge")

    info = status.build_status(tmp_path, cfg, project)
    line = [ln for ln in status.render_status(tmp_path, info).splitlines()
            if ln.startswith("Reviews:")]

    assert info["review"]["rounds"] == 2
    assert len(line) == 1
    assert "2 rounds" in line[0]
    assert "round 2" in line[0]
    assert "ready-to-merge" in line[0]
    assert "2026-08-02" in line[0]                 # the close date, not the start


def test_status_review_line_reports_an_open_round_as_open(tmp_path):
    # REQ-12/REQ-19: the latest round is the highest-numbered one, open or not,
    # and an open one shows its start date rather than a verdict.
    from specflo import review
    cfg, project = _plan_at_execute(tmp_path)
    _close_review(tmp_path, cfg, "ready-to-merge")
    _close_review(tmp_path, cfg, "changes-requested")
    review.start_round(tmp_path, cfg, "thing", today="2026-08-09", over_budget=True)

    info = status.build_status(tmp_path, cfg, project)
    line = next(ln for ln in status.render_status(tmp_path, info).splitlines()
                if ln.startswith("Reviews:"))

    assert info["review"]["open"] is True
    assert info["review"]["latest"] == 3
    assert "round 3 open" in line
    assert "2026-08-09" in line
    assert "ready-to-merge" not in line            # round 1's verdict is not current


def _round_two_with_a_regression(tmp_path, cfg):
    """Round 1 closed asking for changes, round 2 open with one should-fix
    marked a regression and round 1's item checked closed."""
    from specflo import review
    _close_review(tmp_path, cfg, "changes-requested")
    review.start_round(tmp_path, cfg, "thing", today="2026-08-03", over_budget=True)
    review.check_finding(tmp_path, cfg, "thing", "F-01", "closed")
    review.add_finding(
        tmp_path, cfg, "thing", "should-fix", "The fix broke it", "src/app.py:2", regression=True
    )


def _reviews_line(tmp_path, info):
    return next(ln for ln in status.render_status(tmp_path, info).splitlines()
                if ln.startswith("Reviews:"))


def test_status_review_line_names_the_latest_rounds_regressions(tmp_path):
    from specflo import review
    cfg, project = _plan_at_execute(tmp_path)
    _round_two_with_a_regression(tmp_path, cfg)
    review.close_round(tmp_path, cfg, "thing", today="2026-08-04")

    info = status.build_status(tmp_path, cfg, project)

    assert info["review"]["regressions"] == 1
    assert _reviews_line(tmp_path, info) == (
        "Reviews: 2 rounds; latest round 2 changes-requested (2026-08-04); 1 regression"
    )


def test_status_review_line_names_an_open_rounds_regressions_so_far(tmp_path):
    cfg, project = _plan_at_execute(tmp_path)
    _round_two_with_a_regression(tmp_path, cfg)

    info = status.build_status(tmp_path, cfg, project)

    assert info["review"]["regressions"] == 1
    assert _reviews_line(tmp_path, info) == (
        "Reviews: 2 rounds; latest round 2 open (started 2026-08-03); 1 regression"
    )


def test_status_review_line_names_no_regressions_for_a_round_with_none(tmp_path):
    cfg, project = _plan_at_execute(tmp_path)
    _close_review(tmp_path, cfg, "changes-requested")

    info = status.build_status(tmp_path, cfg, project)

    assert info["review"]["regressions"] == 0
    assert _reviews_line(tmp_path, info) == (
        "Reviews: 1 round; latest round 1 changes-requested (2026-08-02)"
    )


def test_status_review_line_counts_nothing_in_a_waived_round(tmp_path):
    # A waive reads no findings, so a marked line in it counts nothing.
    from specflo import review
    cfg, project = _plan_at_execute(tmp_path)
    _round_two_with_a_regression(tmp_path, cfg)
    review.close_round(tmp_path, cfg, "thing", "waived", reason="By hand", today="2026-08-04")

    info = status.build_status(tmp_path, cfg, project)

    assert info["review"]["regressions"] is None
    assert "regression" not in _reviews_line(tmp_path, info)


def test_render_status_reads_a_review_without_a_regression_count(tmp_path):
    # A view from a daemon older than regression marks carries no count.
    cfg, project = _plan_at_execute(tmp_path)
    _close_review(tmp_path, cfg, "changes-requested")
    info = status.build_status(tmp_path, cfg, project)
    del info["review"]["regressions"]

    assert _reviews_line(tmp_path, info) == (
        "Reviews: 1 round; latest round 1 changes-requested (2026-08-02)"
    )


def test_status_has_no_review_line_before_any_round_exists(tmp_path):
    cfg, project = _plan_at_execute(tmp_path)
    info = status.build_status(tmp_path, cfg, project)
    assert "review" not in info
    assert "Reviews:" not in status.render_status(tmp_path, info)


# --- round kinds on the review line -------------------------------------------
# A project of gate rounds reads as it always has. Once a harden round is
# recorded, the line counts each kind and the latest round's stamp names its own.


def _harden_review(tmp_path, cfg, close=True):
    """Open a harden round with no findings, closing it hardened unless it
    should stay open; its path."""
    import reviewhelp
    from specflo import review
    path, _ = review.start_round(tmp_path, cfg, "thing", today="2026-08-03", harden=True)
    if close:
        reviewhelp.write_none(path)
        review.close_round(tmp_path, cfg, "thing", today="2026-08-04")
    return path


def test_status_review_line_names_each_kind_once_a_harden_round_exists(tmp_path):
    cfg, project = _plan_at_execute(tmp_path)
    _close_review(tmp_path, cfg, "ready-to-merge")
    _harden_review(tmp_path, cfg)

    info = status.build_status(tmp_path, cfg, project)

    assert info["review"]["kinds"] == ["gate", "harden"]
    assert _reviews_line(tmp_path, info) == (
        "Reviews: 2 rounds (1 gate, 1 harden); latest round 2 hardened"
        " (harden round, 2026-08-04); passes"
    )


def test_status_review_line_names_the_kind_of_an_open_or_waived_latest_round(tmp_path):
    from specflo import review
    cfg, project = _plan_at_execute(tmp_path)
    _harden_review(tmp_path, cfg)
    _harden_review(tmp_path, cfg, close=False)

    info = status.build_status(tmp_path, cfg, project)

    assert _reviews_line(tmp_path, info) == (
        "Reviews: 2 rounds (0 gate, 2 harden); latest round 2 open"
        " (harden round, started 2026-08-03)"
    )
    review.close_round(tmp_path, cfg, "thing", "waived", reason="By hand", today="2026-08-05")
    _close_review(tmp_path, cfg, "waived", reason="By hand")
    info = status.build_status(tmp_path, cfg, project)
    assert info["review"]["kinds"] == ["harden", "harden", "gate"]
    assert _reviews_line(tmp_path, info) == (
        "Reviews: 3 rounds (1 gate, 2 harden); latest round 3 waived (gate round, 2026-08-02)"
    )


def test_status_of_gate_rounds_alone_carries_no_kinds(tmp_path):
    # The review block stays the review state as it always was.
    from specflo import review
    cfg, project = _plan_at_execute(tmp_path)
    _close_review(tmp_path, cfg, "changes-requested")

    info = status.build_status(tmp_path, cfg, project)

    assert info["review"] == review.review_state(tmp_path, cfg, "thing")
    assert "kinds" not in info["review"]
    assert "gate" not in _reviews_line(tmp_path, info)


def test_status_reads_a_view_without_kinds_as_gate_rounds_alone(tmp_path):
    # A view from a daemon older than harden rounds carries no kinds.
    cfg, project = _plan_at_execute(tmp_path)
    _close_review(tmp_path, cfg, "ready-to-merge")
    _harden_review(tmp_path, cfg)
    info = status.build_status(tmp_path, cfg, project)
    del info["review"]["kinds"]

    assert _reviews_line(tmp_path, info) == (
        "Reviews: 2 rounds; latest round 2 hardened (2026-08-04); passes"
    )


def test_a_hosted_status_and_checkpoint_name_the_round_kinds_as_local_ones(
    tmp_path, monkeypatch, live_daemon
):
    """A gate round asks for changes, a gate round closes ready, and a harden
    round raises should-fix F-03: status and the checkpoint name both kinds,
    and the checkpoint lists the harden round to read first, locally and
    hosted alike."""
    from test_hosted_parity import _fix, _hosted_steps, _local_steps
    from test_review_settle import _round_one

    shown = [(["status"], None), (["checkpoint"], None), (["doc", "show", "checkpoint"], None)]
    steps = [
        *_round_one(),
        *_fix("Name the right command", "F-01", "T-02"),
        (["review", "start"], None),
        (["review", "finding", "check", "F-01", "closed"], None),
        (["review", "finding", "add", "--severity", "nit", "--text", "A name is vague"], None),
        (["review", "done"], None),
        (["review", "start", "--harden"], None),
        (["review", "finding", "add", "--severity", "should-fix", "--at", "src/app.py:5",
          "--text", "The lock is dropped early"], None),
        (["review", "done"], None),
        *shown,
    ]
    local, _ = _local_steps(tmp_path, monkeypatch, steps)
    hosted, _ = _hosted_steps(tmp_path, monkeypatch, live_daemon, steps)

    assert len(local) == len(hosted) == len(steps) + 1
    for mine, theirs in zip(local[1:], hosted[1:], strict=True):
        assert mine == theirs, f"{' '.join(mine[0])}:\nlocal:\n{mine[2]}\nhosted:\n{theirs[2]}"
    text, printed, stored = (out for _, _, out in hosted[-3:])
    line = next(ln for ln in text.splitlines() if ln.startswith("Reviews:"))
    assert line.startswith(
        "Reviews: 3 rounds (2 gate, 1 harden); latest round 3 hardened (harden round, "
    )
    assert f"\n{line}\n" in printed
    read_first = printed.split("## Read first")[1].split("## Do next")[0]
    assert "- parity-thing/review-3\n" in read_first
    assert printed == stored + "\n"


def test_status_review_state_never_reaches_project_md(tmp_path):
    # REQ-09: round files are the only store; project.md gains no review keys.
    import yaml
    cfg, project = _plan_at_execute(tmp_path)
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    before = set(yaml.safe_load(proj_md.read_text().split("---", 2)[1]))

    _close_review(tmp_path, cfg, "ready-to-merge")

    assert set(yaml.safe_load(proj_md.read_text().split("---", 2)[1])) == before


def test_status_review_state_follows_a_deleted_round_file(tmp_path):
    # REQ-09: derived, not mirrored -- removing the file removes the state.
    cfg, project = _plan_at_execute(tmp_path)
    path = _close_review(tmp_path, cfg, "ready-to-merge")
    assert status.build_status(tmp_path, cfg, project)["review"]["rounds"] == 1

    path.unlink()

    assert "review" not in status.build_status(tmp_path, cfg, project)


# --- execution mode surfaces (fan-out-plans REQ-03) ------------------------


def _project_with_execution(tmp_path, mode):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-07-06", execution=mode)
    projects.switch_project(tmp_path, cfg, "Thing")
    return cfg, projects.load_project(tmp_path, cfg, "thing")


@pytest.mark.parametrize("mode", ["linear", "fan-out"])
def test_build_status_carries_the_execution_mode(tmp_path, mode):
    cfg, project = _project_with_execution(tmp_path, mode)
    info = status.build_status(tmp_path, cfg, project)
    assert info["execution"] == mode


@pytest.mark.parametrize("mode", ["linear", "fan-out"])
def test_render_status_prints_execution_after_phase(tmp_path, mode):
    cfg, project = _project_with_execution(tmp_path, mode)
    info = status.build_status(tmp_path, cfg, project)
    lines = status.render_status(tmp_path, info).splitlines()
    phase_idx = next(i for i, l in enumerate(lines) if l.startswith("Phase:"))
    assert lines[phase_idx + 1] == f"Execution: {mode}"


@pytest.mark.parametrize("mode", ["linear", "fan-out"])
def test_status_command_json_reports_execution(tmp_path, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    _project_with_execution(tmp_path, mode)
    result = CliRunner().invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["execution"] == mode


def test_status_reports_linear_for_a_project_md_without_the_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg, _project = _project_with_execution(tmp_path, "fan-out")
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    proj_md.write_text("\n".join(
        l for l in proj_md.read_text().splitlines() if not l.startswith("execution:")
    ) + "\n")
    result = CliRunner().invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["execution"] == "linear"


# --- levels -------------------------------------------------------------------


def _project_at_level(tmp_path, level):
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["new", "Thing", "--level", level]).exit_code == 0


@pytest.mark.parametrize("level", ["quick", "fast", "full", "harden"])
def test_status_prints_the_level(tmp_path, monkeypatch, level):
    monkeypatch.chdir(tmp_path)
    _project_at_level(tmp_path, level)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert f"Level: {level}" in result.output.splitlines()


@pytest.mark.parametrize("level", ["quick", "fast", "full", "harden"])
def test_status_json_reports_the_level(tmp_path, monkeypatch, level):
    monkeypatch.chdir(tmp_path)
    _project_at_level(tmp_path, level)
    result = CliRunner().invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["level"] == level


def test_render_status_reads_a_view_without_a_level_as_full(tmp_path):
    info = {
        "initialized": True, "active_project": "thing", "name": "Thing",
        "dir": str(tmp_path), "remote": None, "phase": "brainstorm",
        "status": "active", "execution": "linear", "next_step": "x",
        "checkpoint_locator": "thing/checkpoint",
    }
    assert "Level: full" in status.render_status(tmp_path, info).splitlines()
