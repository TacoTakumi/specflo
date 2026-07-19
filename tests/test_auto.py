"""Tests for `specflo auto` and the auto-mode handoff payload (`auto.py`).

T-01 lays down the skeleton: a `specflo auto` command and an `auto.py` that
emits an auto-mode *bootstrap* section for the current phase. It is strictly
additive (REQ-02) and harness-neutral (REQ-05) - the source-scan tests below
pin those invariants so later tasks can't quietly break them.
"""

import io
import tokenize
from pathlib import Path

from typer.testing import CliRunner

from specflo import auto, brainstorm, checkpoint, config, hook, projects, spec, workflow
from specflo.cli import app

runner = CliRunner()

PHASES = ["brainstorm", "spec", "plan", "execute"]


def _active_at(tmp_path, phase, name="My Thing"):
    """Init a repo with one active project advanced to ``phase``; return its slug."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, name)
    projects.switch_project(tmp_path, cfg, name)  # sets active + persists to disk
    slug = "my-thing"
    for _ in range(PHASES.index(phase)):  # advance the phase pointer (bypasses gates)
        projects.advance_project(tmp_path, cfg, slug)
    return slug


def _clause(block, marker):
    """The single bootstrap line carrying ``marker`` (or "" if absent)."""
    for line in block.splitlines():
        if marker in line:
            return line
    return ""


# --- payload derivation -------------------------------------------------------

def test_auto_text_emits_bootstrap_at_each_phase(tmp_path):
    # invoked at any of the four phases, the payload carries the bootstrap marker
    # and names the phase it is bootstrapping.
    for phase in PHASES:
        subdir = tmp_path / phase
        subdir.mkdir()
        _active_at(subdir, phase)
        out = auto.auto_text(subdir)
        assert auto.BOOTSTRAP_MARKER in out, f"no bootstrap section at {phase}"
        assert phase in out, f"payload does not name the {phase} phase"


def test_auto_bootstrap_names_phase_and_marker():
    # the bootstrap builder is a pure function of the phase and always leads with
    # the fixed marker (structural, not verbatim, contract for later tasks).
    for phase in PHASES:
        block = auto.auto_bootstrap(phase)
        assert block.startswith(auto.BOOTSTRAP_MARKER)
        assert phase in block


def test_auto_text_noop_outside_specflo_repo(tmp_path):
    # no .specflo anywhere up the tree -> nothing to emit, and never raises.
    assert auto.auto_text(tmp_path) == ""


def test_auto_text_noop_no_active_project(tmp_path):
    config.init_config(tmp_path)  # initialized, but no project created/activated
    assert auto.auto_text(tmp_path) == ""


def test_auto_text_defaults_to_cwd(tmp_path, monkeypatch):
    _active_at(tmp_path, "brainstorm")
    monkeypatch.chdir(tmp_path)
    assert auto.BOOTSTRAP_MARKER in auto.auto_text()  # no-arg call resolves cwd


# --- T-02: phase-boundary pause override (REQ-06) -----------------------------

def test_bootstrap_overrides_phase_boundary_pause():
    block = auto.auto_bootstrap("brainstorm")
    assert auto.BOUNDARY_OVERRIDE_MARKER in block
    # names the flow the override unblocks (brainstorm -> ... -> execute)
    assert "brainstorm -> spec -> plan -> execute" in block


def test_bootstrap_override_present_at_every_phase():
    for phase in PHASES:
        assert auto.BOUNDARY_OVERRIDE_MARKER in auto.auto_bootstrap(phase)


def test_non_auto_reseed_still_surfaces_pause_and_carries_no_auto_override(tmp_path):
    # REQ-06/REQ-02: the manual reseed is byte-for-byte the ask-first payload
    # (confirmation-gate directive + verbatim checkpoint) and carries none of the
    # auto boundary-override text - the override is confined to auto mode.
    _active_at(tmp_path, "spec")
    cfg = config.load_config(tmp_path)
    project = projects.load_project(tmp_path, cfg, "my-thing")
    out = hook.reseed_text(tmp_path)
    body = checkpoint.render_checkpoint(
        checkpoint.build_checkpoint(tmp_path, project, cfg=cfg)
    )
    assert out == f"{hook.CONFIRMATION_DIRECTIVE}\n\n{body}"  # byte-unchanged
    assert auto.BOUNDARY_OVERRIDE_MARKER not in out


# --- T-03: default decision-fork policy (REQ-11) ------------------------------

def test_bootstrap_fork_policy_takes_and_records_defensible_default():
    # on a fork with a defensible default: take it and record it, naming the
    # `specflo decision add` recording mechanism.
    block = auto.auto_bootstrap("plan")
    assert auto.FORK_POLICY_MARKER in block
    assert "decision add" in block
    assert "defensible default" in block.lower()


def test_bootstrap_fork_policy_stops_and_asks_when_no_default():
    # the other branch: stop and ask the human only when no defensible default
    # exists.
    block = auto.auto_bootstrap("plan")
    lowered = block.lower()
    assert "no defensible default" in lowered
    assert "ask" in lowered


def test_bootstrap_fork_policy_present_at_every_phase():
    for phase in PHASES:
        assert auto.FORK_POLICY_MARKER in auto.auto_bootstrap(phase)


# --- T-04: --autonomy level + irreversible-action gating (REQ-08) -------------

def test_autonomy_levels_and_default():
    assert auto.AUTONOMY_LEVELS == ("safe", "autonomous", "yolo")
    assert auto.DEFAULT_AUTONOMY == "safe"
    # the config-key default matches (no flag / no config -> safe)
    assert config.SpecfloConfig().autonomy == "safe"


def test_bootstrap_safe_and_autonomous_stop_on_irreversible():
    for level in ("safe", "autonomous"):
        block = auto.auto_bootstrap("execute", autonomy=level)
        clause = _clause(block, auto.SIDE_EFFECT_MARKER)
        assert clause, f"no side-effect clause at {level}"
        low = clause.lower()
        assert "stop" in low and "hand off" in low
        assert "permit" not in low


def test_bootstrap_yolo_permits_irreversible():
    clause = _clause(
        auto.auto_bootstrap("execute", autonomy="yolo"), auto.SIDE_EFFECT_MARKER
    )
    assert "permit" in clause.lower()


def test_bootstrap_defaults_to_safe_side_effect_gate():
    # no autonomy arg -> the safe (stop-on-irreversible) payload, verbatim.
    assert auto.auto_bootstrap("execute") == auto.auto_bootstrap("execute", autonomy="safe")


def test_auto_text_unknown_level_falls_back_to_safe(tmp_path):
    _active_at(tmp_path, "execute")
    out = auto.auto_text(tmp_path, autonomy="bogus")
    assert "permit" not in _clause(out, auto.SIDE_EFFECT_MARKER).lower()


def test_config_persists_non_default_autonomy_and_omits_the_default(tmp_path):
    config.init_config(tmp_path)
    # a default (safe) config carries no autonomy key at all
    assert "autonomy" not in config.config_path(tmp_path).read_text()
    cfg = config.load_config(tmp_path)
    cfg.autonomy = "yolo"
    config.save_config(tmp_path, cfg)
    assert config.load_config(tmp_path).autonomy == "yolo"  # survives round-trip


# --- T-06: decision delegation by --autonomy level (REQ-12) -------------------

def test_fork_policy_safe_stops_on_unresolvable():
    clause = _clause(auto.auto_bootstrap("plan", autonomy="safe"), auto.FORK_POLICY_MARKER)
    low = clause.lower()
    assert "no defensible default" in low
    assert "stop" in low and "ask" in low  # stop-and-ask
    assert "decision add" in clause


def test_fork_policy_delegated_decides_and_records_on_ambiguous():
    for level in ("autonomous", "yolo"):
        clause = _clause(
            auto.auto_bootstrap("plan", autonomy=level), auto.FORK_POLICY_MARKER
        )
        assert clause, f"no fork clause at {level}"
        assert "decision add" in clause  # still records each assumption
        low = clause.lower()
        assert "even" in low  # decide-and-record *even* on an ambiguous fork


def test_fork_policy_differs_between_safe_and_delegated():
    safe = _clause(auto.auto_bootstrap("plan", autonomy="safe"), auto.FORK_POLICY_MARKER)
    for level in ("autonomous", "yolo"):
        deleg = _clause(auto.auto_bootstrap("plan", autonomy=level), auto.FORK_POLICY_MARKER)
        assert safe != deleg  # observable difference between the levels


# --- T-08: completion-signal termination (REQ-13) -----------------------------

def _complete(tmp_path, name="My Thing"):
    """Active project driven (via the projects layer) to execute, then completed."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, name)
    projects.switch_project(tmp_path, cfg, name)
    for _ in range(3):  # brainstorm -> spec -> plan -> execute
        projects.advance_project(tmp_path, cfg, "my-thing")
    projects.complete_project(tmp_path, cfg, "my-thing")
    return cfg


def test_bootstrap_names_the_completion_signal():
    assert auto.COMPLETION_SIGNAL == "Completed project"
    block = auto.auto_bootstrap("execute")
    assert auto.COMPLETION_MARKER in block
    assert auto.COMPLETION_SIGNAL in block


def test_auto_on_complete_project_stops_without_continue(tmp_path):
    _complete(tmp_path)
    out = auto.auto_text(tmp_path)
    assert out == auto.AUTO_COMPLETE_DIRECTIVE
    assert auto.BOOTSTRAP_MARKER not in out  # no continue/bootstrap directive
    low = out.lower()
    assert "complete" in low and "stop" in low
    assert "continue" not in low


def test_cli_advance_emits_the_completion_signal_the_bootstrap_names(tmp_path, monkeypatch):
    # end-to-end: the terminal `advance` prints exactly the string auto keys on.
    monkeypatch.chdir(tmp_path)
    from test_cli import _project_at_execute

    _project_at_execute(runner, app, tmp_path)
    runner.invoke(app, ["task", "start", "T-01"])
    runner.invoke(app, ["task", "done", "T-01"])
    result = runner.invoke(app, ["advance"])  # execute -> complete
    assert auto.COMPLETION_SIGNAL in result.stdout


# --- T-09: iteration/step cap + durable auto-run state (REQ-14) ----------------

def test_max_passes_default_is_a_documented_number():
    assert isinstance(config.DEFAULT_MAX_PASSES, int)
    assert config.DEFAULT_MAX_PASSES == 50
    assert config.SpecfloConfig().auto_max_passes == config.DEFAULT_MAX_PASSES


def test_auto_pass_counts_up_and_escalates_at_cap(tmp_path):
    _active_at(tmp_path, "execute")
    cap = 3
    for _ in range(cap - 1):  # passes below the cap -> continue directive
        out = auto.auto_pass(tmp_path, max_passes=cap)
        assert auto.BOOTSTRAP_MARKER in out
        assert auto.ESCALATION_MARKER not in out
    out = auto.auto_pass(tmp_path, max_passes=cap)  # the pass that reaches the cap
    assert auto.ESCALATION_MARKER in out
    assert auto.BOOTSTRAP_MARKER not in out
    assert str(cap) in out  # names the cap it hit


def test_auto_pass_persists_counter_across_calls(tmp_path):
    _active_at(tmp_path, "execute")
    auto.auto_pass(tmp_path, max_passes=10)
    auto.auto_pass(tmp_path, max_passes=10)
    cfg = config.load_config(tmp_path)
    assert auto.load_run_state(tmp_path, cfg, "my-thing")["passes"] == 2


def test_run_state_lives_in_a_dedicated_file_not_config(tmp_path):
    _active_at(tmp_path, "execute")
    auto.auto_pass(tmp_path, max_passes=10)
    # the counter is in a dedicated run-state file, never a config auto-on key
    assert "passes" not in config.config_path(tmp_path).read_text()
    cfg = config.load_config(tmp_path)
    assert auto.run_state_path(tmp_path, cfg, "my-thing").is_file()


def test_cli_auto_drives_to_cap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    r1 = runner.invoke(app, ["auto", "--max-passes", "2"])
    assert auto.BOOTSTRAP_MARKER in r1.stdout and auto.ESCALATION_MARKER not in r1.stdout
    r2 = runner.invoke(app, ["auto", "--max-passes", "2"])
    assert auto.ESCALATION_MARKER in r2.stdout and auto.BOOTSTRAP_MARKER not in r2.stdout


def test_cli_auto_rejects_nonpositive_max_passes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    assert runner.invoke(app, ["auto", "--max-passes", "0"]).exit_code != 0


def test_config_persists_non_default_cap_and_omits_the_default(tmp_path):
    config.init_config(tmp_path)
    assert "auto_max_passes" not in config.config_path(tmp_path).read_text()
    cfg = config.load_config(tmp_path)
    cfg.auto_max_passes = 7
    config.save_config(tmp_path, cfg)
    assert config.load_config(tmp_path).auto_max_passes == 7  # survives round-trip


def test_readme_and_changelog_document_the_cap():
    repo = Path(__file__).resolve().parents[1]
    for doc in ((repo / "README.md").read_text(), (repo / "CHANGELOG.md").read_text()):
        assert "--max-passes" in doc
        assert str(config.DEFAULT_MAX_PASSES) in doc


# --- T-10: stall detection (REQ-15) -------------------------------------------

def test_stall_threshold_is_a_source_constant():
    # N is defined in source (not a user knob) and trips well before the pass cap
    assert isinstance(auto.STALL_THRESHOLD, int)
    assert auto.STALL_THRESHOLD >= 1
    assert auto.STALL_THRESHOLD < config.DEFAULT_MAX_PASSES


def test_progress_signal_changes_when_phase_advances(tmp_path):
    # the forward-progress signal is derived from real project state: advancing
    # the phase (forward progress) changes it.
    _active_at(tmp_path, "brainstorm")
    cfg = config.load_config(tmp_path)
    before = auto.progress_signal(
        tmp_path, cfg, projects.load_project(tmp_path, cfg, "my-thing")
    )
    projects.advance_project(tmp_path, cfg, "my-thing")  # brainstorm -> spec
    after = auto.progress_signal(
        tmp_path, cfg, projects.load_project(tmp_path, cfg, "my-thing")
    )
    assert before != after


def test_auto_pass_records_progress_signal_in_run_state(tmp_path):
    # each pass records the phase forward-progress signal in the auto-run state.
    _active_at(tmp_path, "execute")
    auto.auto_pass(tmp_path, max_passes=1000)
    cfg = config.load_config(tmp_path)
    state = auto.load_run_state(tmp_path, cfg, "my-thing")
    assert state.get("progress_signal")
    assert "execute" in state["progress_signal"]


def test_auto_pass_escalates_after_n_unchanged_passes(tmp_path):
    # a phase that makes no forward progress across N consecutive passes escalates
    # (a stop/escalate outcome, not a continue directive), naming the stall.
    _active_at(tmp_path, "execute")
    n = auto.STALL_THRESHOLD
    for _ in range(n):  # baseline + (n-1) unchanged passes stay below the trip
        out = auto.auto_pass(tmp_path, max_passes=1000)  # big cap: isolate stall
        assert auto.BOOTSTRAP_MARKER in out
        assert auto.ESCALATION_MARKER not in out
    out = auto.auto_pass(tmp_path, max_passes=1000)  # the Nth unchanged pass trips
    assert auto.ESCALATION_MARKER in out
    assert auto.BOOTSTRAP_MARKER not in out
    assert "progress" in out.lower()  # a stall, distinct from the pass-cap message


def test_auto_pass_does_not_stall_while_phase_progresses(tmp_path):
    # a progressing phase never trips stall detection, even across more passes
    # than the threshold: a changing signal resets the streak every pass.
    _active_at(tmp_path, "brainstorm")
    cfg = config.load_config(tmp_path)
    for _ in range(auto.STALL_THRESHOLD + 1):
        out = auto.auto_pass(tmp_path, max_passes=1000)
        assert auto.BOOTSTRAP_MARKER in out
        assert auto.ESCALATION_MARKER not in out
        proj = projects.load_project(tmp_path, cfg, "my-thing")
        if workflow.next_phase(proj.phase) is not None:
            projects.advance_project(tmp_path, cfg, "my-thing")  # forward progress


# --- T-11: kill switch (REQ-16) -----------------------------------------------

def test_kill_switch_stops_next_pass_and_clearing_resumes(tmp_path):
    # set the durable auto-off flag -> the next pass stops (no continue directive);
    # clearing it restores normal auto continuation.
    _active_at(tmp_path, "execute")
    auto.set_kill_switch(tmp_path, killed=True)
    stopped = auto.auto_pass(tmp_path, max_passes=1000)
    assert auto.KILL_MARKER in stopped
    assert auto.BOOTSTRAP_MARKER not in stopped
    auto.set_kill_switch(tmp_path, killed=False)
    resumed = auto.auto_pass(tmp_path, max_passes=1000)
    assert auto.BOOTSTRAP_MARKER in resumed
    assert auto.KILL_MARKER not in resumed


def test_kill_switch_is_durable_in_run_state_not_config(tmp_path):
    _active_at(tmp_path, "execute")
    auto.set_kill_switch(tmp_path, killed=True)
    cfg = config.load_config(tmp_path)
    assert auto.load_run_state(tmp_path, cfg, "my-thing").get("killed") is True
    # the flag lives only in the dedicated run-state file, never a config key (REQ-01)
    assert "killed" not in config.config_path(tmp_path).read_text()


def test_kill_switch_clearing_removes_the_flag(tmp_path):
    _active_at(tmp_path, "execute")
    auto.set_kill_switch(tmp_path, killed=True)
    auto.set_kill_switch(tmp_path, killed=False)
    cfg = config.load_config(tmp_path)
    assert "killed" not in auto.load_run_state(tmp_path, cfg, "my-thing")


def test_killed_pass_does_not_advance_the_pass_counter(tmp_path):
    # a killed pass is a halt, not a forward pass - it must not consume the cap.
    _active_at(tmp_path, "execute")
    auto.set_kill_switch(tmp_path, killed=True)
    auto.auto_pass(tmp_path, max_passes=1000)
    cfg = config.load_config(tmp_path)
    assert auto.load_run_state(tmp_path, cfg, "my-thing").get("passes", 0) == 0


def test_cli_auto_off_stops_then_on_resumes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    assert runner.invoke(app, ["auto", "--off"]).exit_code == 0
    stopped = runner.invoke(app, ["auto"])
    assert auto.KILL_MARKER in stopped.stdout
    assert auto.BOOTSTRAP_MARKER not in stopped.stdout
    assert runner.invoke(app, ["auto", "--on"]).exit_code == 0
    resumed = runner.invoke(app, ["auto"])
    assert auto.BOOTSTRAP_MARKER in resumed.stdout
    assert auto.KILL_MARKER not in resumed.stdout


def test_cli_auto_off_and_on_are_mutually_exclusive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    assert runner.invoke(app, ["auto", "--off", "--on"]).exit_code != 0


# --- T-10 follow-up: finer within-phase progress signal (brainstorm/spec) -----
# The initial signal only read plan/execute task counts, so brainstorm and spec
# were phase-coarse ("<phase>:0/0") and only a phase advance moved them - a stall
# false-positive for a multi-pass brainstorm/spec. The signal now counts recorded
# decisions (brainstorm) / requirements (spec) so real within-phase work moves it.

def test_progress_signal_reflects_decision_count_in_brainstorm(tmp_path):
    slug = _active_at(tmp_path, "brainstorm")
    cfg = config.load_config(tmp_path)
    brainstorm.start_brainstorm(tmp_path, cfg, slug)
    proj = projects.load_project(tmp_path, cfg, slug)
    before = auto.progress_signal(tmp_path, cfg, proj)
    brainstorm.add_decision(tmp_path, cfg, slug, text="use argparse", rationale="stdlib")
    assert auto.progress_signal(tmp_path, cfg, proj) != before  # a decision is progress


def test_progress_signal_reflects_requirement_count_in_spec(tmp_path):
    slug = _active_at(tmp_path, "spec")
    cfg = config.load_config(tmp_path)
    spec.start_spec(tmp_path, cfg, slug)
    proj = projects.load_project(tmp_path, cfg, slug)
    before = auto.progress_signal(tmp_path, cfg, proj)
    spec.add_requirement(tmp_path, cfg, slug, text="prints greeting", acceptance="exit 0")
    assert auto.progress_signal(tmp_path, cfg, proj) != before  # a requirement is progress


def test_auto_pass_does_not_stall_while_decisions_accumulate(tmp_path):
    # the false-positive the dry-run surfaced: recording a decision each pass is
    # forward progress, so more passes than the stall threshold must NOT escalate.
    slug = _active_at(tmp_path, "brainstorm")
    cfg = config.load_config(tmp_path)
    brainstorm.start_brainstorm(tmp_path, cfg, slug)
    for i in range(auto.STALL_THRESHOLD + 2):
        brainstorm.add_decision(tmp_path, cfg, slug, text=f"decision {i}", rationale="r")
        out = auto.auto_pass(tmp_path, max_passes=1000)
        assert auto.BOOTSTRAP_MARKER in out
        assert auto.ESCALATION_MARKER not in out


# --- T-07: hardcoded always-stop floor (REQ-09) -------------------------------

def test_floor_is_source_constant_and_sensible():
    assert isinstance(auto.ALWAYS_STOP_FLOOR, tuple)
    assert auto.ALWAYS_STOP_FLOOR  # non-empty
    joined = " ".join(auto.ALWAYS_STOP_FLOOR).lower()
    # aligned with the global never-post/publish/send rule
    assert "push" in joined
    assert "secret" in joined or "credential" in joined


def test_floor_named_at_every_level_including_yolo():
    for level in auto.AUTONOMY_LEVELS:
        block = auto.auto_bootstrap("execute", autonomy=level)
        assert auto.FLOOR_MARKER in block
        clause = _clause(block, auto.FLOOR_MARKER)
        for item in auto.ALWAYS_STOP_FLOOR:  # every floor item is named
            assert item in clause, f"{item!r} missing from floor clause at {level}"


def test_floor_not_disabled_by_config_default_yolo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    cfg = config.load_config(tmp_path)
    cfg.autonomy = "yolo"
    config.save_config(tmp_path, cfg)  # config default yolo, no flag
    result = runner.invoke(app, ["auto"])
    assert auto.FLOOR_MARKER in result.stdout


def test_floor_not_read_from_user_config(tmp_path):
    # the floor is a source constant; config carries no key that could shrink it
    config.init_config(tmp_path)
    assert "floor" not in config.config_path(tmp_path).read_text().lower()


# --- T-05: plan-time avoidance directive (REQ-10) -----------------------------

def test_bootstrap_plan_time_deferred_draft_and_handoff():
    block = auto.auto_bootstrap("plan")
    assert auto.PLAN_TIME_MARKER in block
    low = block.lower()
    assert "defer" in low            # deferred
    assert "draft" in low and "hand" in low  # draft-and-handoff


def test_bootstrap_plan_time_present_at_every_phase():
    for phase in PHASES:
        assert auto.PLAN_TIME_MARKER in auto.auto_bootstrap(phase)


# --- T-13: self-contained three-part reseed payload (REQ-03) ------------------
# `specflo auto` now emits a payload a freshly-cleared session can act on without
# re-deriving state: (1) the auto-mode bootstrap, (2) a substring byte-for-byte
# equal to `specflo checkpoint` (render_checkpoint) output, and (3) a compact
# next-step block specflo generates from the current phase (naming the phase and
# its immediate next action, pointing at - not depending on - the phase skill).

def _checkpoint_text(cwd, slug="my-thing"):
    """`render_checkpoint(build_checkpoint(...))` for the active project at ``cwd``."""
    cfg = config.load_config(cwd)
    project = projects.load_project(cwd, cfg, slug)
    return checkpoint.render_checkpoint(
        checkpoint.build_checkpoint(cwd, project, cfg=cfg)
    )


def test_auto_payload_has_all_three_parts_at_each_phase(tmp_path):
    # at every phase the payload carries the bootstrap, the verbatim checkpoint as
    # an exact substring, and the generated next-step block naming the phase.
    for phase in PHASES:
        subdir = tmp_path / phase
        subdir.mkdir()
        _active_at(subdir, phase)
        out = auto.auto_text(subdir)
        assert auto.BOOTSTRAP_MARKER in out, f"part 1 (bootstrap) missing at {phase}"
        assert _checkpoint_text(subdir) in out, f"part 2 (checkpoint) not verbatim at {phase}"
        assert auto.NEXT_STEP_MARKER in out, f"part 3 (next-step) missing at {phase}"
        block = out.split(auto.NEXT_STEP_MARKER, 1)[1]
        assert phase in block, f"next-step block does not name the {phase} phase"


def test_next_step_block_names_phase_action_and_points_at_skill():
    # the block builder is a pure function: leads with the marker, names the phase,
    # carries the immediate next action, and points at the phase skill by name.
    for phase in PHASES:
        do_next = "SENTINEL NEXT ACTION"
        block = auto.next_step_block(phase, do_next)
        assert block.startswith(auto.NEXT_STEP_MARKER)
        assert phase in block
        assert do_next in block                      # immediate next action carried
        assert auto.PHASE_SKILLS[phase] in block      # points at the phase skill


def test_auto_payload_next_step_carries_derived_do_next(tmp_path):
    # part 3 is generated from the current phase's derived next action, so a fresh
    # session need not re-derive it.
    _active_at(tmp_path, "execute")
    cfg = config.load_config(tmp_path)
    project = projects.load_project(tmp_path, cfg, "my-thing")
    do_next = checkpoint.build_checkpoint(tmp_path, project, cfg=cfg)["do_next"]
    block = auto.auto_text(tmp_path).split(auto.NEXT_STEP_MARKER, 1)[1]
    assert do_next in block


def test_auto_payload_ordering_bootstrap_checkpoint_next_step(tmp_path):
    # the three parts appear in order: bootstrap, then checkpoint, then next-step.
    _active_at(tmp_path, "spec")
    out = auto.auto_text(tmp_path)
    i_boot = out.index(auto.BOOTSTRAP_MARKER)
    i_cp = out.index(_checkpoint_text(tmp_path))
    i_step = out.index(auto.NEXT_STEP_MARKER)
    assert i_boot < i_cp < i_step


def test_auto_pass_continue_emits_three_part_payload(tmp_path):
    # the stateful pass (not a stop directive) emits the full three-part payload.
    _active_at(tmp_path, "execute")
    out = auto.auto_pass(tmp_path, max_passes=1000)
    assert auto.BOOTSTRAP_MARKER in out
    assert _checkpoint_text(tmp_path) in out
    assert auto.NEXT_STEP_MARKER in out


def test_stop_directives_carry_no_reseed_payload(tmp_path):
    # a completed project stops without a continue payload - no checkpoint or
    # next-step block rides a stop directive.
    _complete(tmp_path)
    out = auto.auto_text(tmp_path)
    assert out == auto.AUTO_COMPLETE_DIRECTIVE
    assert auto.NEXT_STEP_MARKER not in out


def test_cli_auto_emits_three_part_payload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["auto"])
    assert result.exit_code == 0
    assert auto.BOOTSTRAP_MARKER in result.stdout
    assert auto.NEXT_STEP_MARKER in result.stdout
    assert _checkpoint_text(tmp_path) in result.stdout


# --- T-14: bootstrap self-propagation (REQ-04) --------------------------------
# The auto policy must not silently revert to the manual ask-first behavior after
# the first boundary: the bootstrap instructs the continuing pass to carry and
# re-emit the auto bootstrap, so a simulated second pass stays auto - never the
# ask-first CONFIRMATION_DIRECTIVE.

def test_bootstrap_self_propagation_marker_present_at_every_phase():
    for phase in PHASES:
        block = auto.auto_bootstrap(phase)
        assert auto.SELF_PROPAGATE_MARKER in block
        clause = _clause(block, auto.SELF_PROPAGATE_MARKER)
        assert "specflo auto" in clause  # names the re-emit mechanism
        low = clause.lower()
        assert "re-emit" in low or "re-propagat" in low or "carry" in low


def test_bootstrap_self_propagation_present_at_every_autonomy_level():
    for level in auto.AUTONOMY_LEVELS:
        assert auto.SELF_PROPAGATE_MARKER in auto.auto_bootstrap("execute", autonomy=level)


def test_second_pass_from_payload_stays_auto_not_ask_first(tmp_path):
    # simulate the outer harness clearing context and continuing: a second pass
    # still yields an auto bootstrap, never the manual ask-first reseed directive.
    _active_at(tmp_path, "brainstorm")
    first = auto.auto_pass(tmp_path, max_passes=1000)
    assert auto.BOOTSTRAP_MARKER in first
    second = auto.auto_pass(tmp_path, max_passes=1000)
    assert auto.BOOTSTRAP_MARKER in second               # still auto
    assert hook.CONFIRMATION_DIRECTIVE not in second     # not the ask-first reseed
    assert auto.SELF_PROPAGATE_MARKER in second          # and it keeps self-propagating


# --- CLI surface --------------------------------------------------------------

def test_cli_auto_no_flag_defaults_to_safe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["auto"])
    assert result.exit_code == 0
    assert "permit" not in _clause(result.stdout, auto.SIDE_EFFECT_MARKER).lower()


def test_cli_auto_yolo_flag_permits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["auto", "--autonomy", "yolo"])
    assert result.exit_code == 0
    assert "permit" in _clause(result.stdout, auto.SIDE_EFFECT_MARKER).lower()


def test_cli_auto_rejects_invalid_autonomy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["auto", "--autonomy", "bogus"])
    assert result.exit_code != 0


def test_cli_auto_config_default_applies_when_no_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    cfg = config.load_config(tmp_path)
    cfg.autonomy = "yolo"
    config.save_config(tmp_path, cfg)  # hand-set the config default
    result = runner.invoke(app, ["auto"])  # no flag -> config default
    assert "permit" in _clause(result.stdout, auto.SIDE_EFFECT_MARKER).lower()


def test_readme_and_changelog_document_autonomy():
    repo = Path(__file__).resolve().parents[1]
    readme = (repo / "README.md").read_text()
    changelog = (repo / "CHANGELOG.md").read_text()
    for doc in (readme, changelog):
        assert "--autonomy" in doc
        for level in auto.AUTONOMY_LEVELS:
            assert level in doc
    # the default is documented
    assert "safe" in readme and "default" in readme.lower()


# --- CLI surface (skeleton) ---------------------------------------------------

def test_auto_appears_in_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "auto" in result.stdout


def test_cli_auto_prints_bootstrap_and_exits_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["auto"])
    assert result.exit_code == 0
    assert auto.BOOTSTRAP_MARKER in result.stdout


# --- invariants: strictly additive (REQ-02), harness-neutral (REQ-05) ---------

_AUTO_SRC = Path(auto.__file__).read_text()


def _auto_code_only() -> str:
    """`auto.py` source with comments and string literals (incl. docstrings) removed.

    Used to assert the *code* never touches a symbol while prose is free to
    explain the invariant.
    """
    tokens = tokenize.generate_tokens(io.StringIO(_AUTO_SRC).readline)
    return " ".join(
        tok.string
        for tok in tokens
        if tok.type not in (tokenize.COMMENT, tokenize.STRING)
    )


def test_auto_source_spawns_no_nested_agent_or_session():
    # REQ-05: specflo emits the payload and launches no nested agent/session.
    for banned in ("subprocess", "os.system", "Popen", "os.exec"):
        assert banned not in _AUTO_SRC, f"auto.py must not reference {banned!r}"


def test_auto_source_embeds_no_harness_trigger_strings():
    # REQ-05: the payload stays harness-neutral - no harness-specific trigger.
    for trigger in ("clearanddo", "claude --print", "--dangerously"):
        assert trigger not in _AUTO_SRC, f"auto.py must not embed trigger {trigger!r}"


def test_auto_source_does_not_touch_reseed_or_advance_gate():
    # REQ-02: auto is a separate surface; its *code* never calls into the
    # ask-first reseed or the advance gate (prose may still name them).
    code = _auto_code_only()
    for banned in ("reseed_text", "CONFIRMATION_DIRECTIVE", "advance_project"):
        assert banned not in code, f"auto.py code must not reference {banned!r}"


def test_auto_adds_no_persisted_auto_on_config_key(tmp_path):
    # REQ-01/D-10: the opt-in is the per-invocation command; no auto-*on* toggle
    # (`auto: true` or equivalent) is persisted. An `autonomy` *level* string is
    # allowed (REQ-08); a boolean on/off switch is not.
    import yaml

    _active_at(tmp_path, "brainstorm")
    auto.auto_text(tmp_path)
    data = yaml.safe_load(config.config_path(tmp_path).read_text()) or {}
    for key, val in data.items():
        assert not (key.startswith("auto") and isinstance(val, bool)), (
            f"unexpected auto-on toggle {key!r} in config"
        )


# --- clear-and-continue: the payload consumes the shared continuation builder ---


def test_phase_skill_map_is_the_shared_one():
    # REQ-04: one source for the phase -> skill map. `auto` re-exports the
    # continuation module's map rather than keeping a second copy that could drift.
    from specflo import continuation

    assert auto.PHASE_SKILLS is continuation.PHASE_SKILLS


def test_next_step_block_delegates_to_the_shared_builder():
    # REQ-04 / D-05: the block is its marker plus the builder's output verbatim -
    # `auto` composes no continuation wording of its own.
    from specflo import continuation

    for phase in PHASES:
        do_next = "SENTINEL NEXT ACTION"
        expected = f"{auto.NEXT_STEP_MARKER}\n{continuation.build_continuation(phase, do_next)}"
        assert auto.next_step_block(phase, do_next) == expected


def test_auto_payload_carries_the_clear_point(tmp_path):
    # D-05: the payload's next-step block carries the full continuation, including
    # the clear-point line. The bootstrap's self-propagation clause outranks the
    # manual resume command for an agent in an auto run, and the line names
    # `specflo auto` parenthetically anyway (D-02), so it cannot mislead.
    from specflo import continuation

    _active_at(tmp_path, "plan")
    out = auto.auto_text(tmp_path)
    assert continuation.CLEAR_POINT_MARKER in out
    assert "`specflo auto`" in out


def test_no_module_duplicates_the_continuation_wording():
    # REQ-04's source-scan half, repo-wide: exactly one module may evaluate the
    # continuation wording. Scanned over executable code so prose about the
    # invariant doesn't count as a violation.
    import importlib
    import pkgutil

    from conftest import executable_identifiers

    import specflo
    from specflo import continuation

    offenders = []
    for info in pkgutil.iter_modules(specflo.__path__):
        if info.name == "continuation":
            continue
        module = importlib.import_module(f"specflo.{info.name}")
        code = executable_identifiers(module)
        if continuation.CLEAR_POINT_MARKER.lower() in code or "resume with" in code:
            offenders.append(info.name)
    assert not offenders, f"continuation wording duplicated in: {offenders}"
