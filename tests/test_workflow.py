import pytest

from specflo import workflow


def test_phases_are_in_workflow_order():
    assert workflow.PHASES == ["brainstorm", "spec", "plan", "execute"]


def test_next_phase_advances_through_the_sequence():
    assert workflow.next_phase("brainstorm") == "spec"
    assert workflow.next_phase("spec") == "plan"
    assert workflow.next_phase("plan") == "execute"


def test_next_phase_is_none_at_the_final_phase():
    assert workflow.next_phase("execute") is None


def test_next_step_gives_a_distinct_nonempty_hint_per_phase():
    steps = {phase: workflow.next_step(phase) for phase in workflow.PHASES}
    assert all(isinstance(s, str) and s.strip() for s in steps.values())
    assert len(set(steps.values())) == len(workflow.PHASES)


def test_unknown_phase_raises_value_error():
    with pytest.raises(ValueError):
        workflow.next_phase("nonsense")
    with pytest.raises(ValueError):
        workflow.next_step("nonsense")


def test_next_step_single_arg_unchanged():
    for phase in workflow.PHASES:
        assert isinstance(workflow.next_step(phase), str)
    # plan/execute base strings unchanged when called single-arg
    assert workflow.next_step("execute") == workflow.next_step("execute", progress=None)


def test_next_step_execute_is_progress_aware():
    pending = {"total": 2, "all_done": False, "next_actionable": ["T-01"]}
    assert "T-01" in workflow.next_step("execute", progress=pending)
    done = {"total": 2, "all_done": True, "next_actionable": []}
    assert "final" in workflow.next_step("execute", progress=done).lower()
    blocked = {"total": 2, "all_done": False, "next_actionable": []}
    assert "actionable" in workflow.next_step("execute", progress=blocked).lower()
    assert "complete" in workflow.next_step("execute", complete=True).lower()


def test_next_step_shelved_directs_to_resume_or_new():
    msg = workflow.next_step("plan", shelved=True)
    low = msg.lower()
    assert "resume" in low  # offers resume
    assert "new" in low     # ...or a new project
    # shelved is orthogonal to phase: same hint at any phase, taking precedence
    assert workflow.next_step("execute", shelved=True) == msg
    assert workflow.next_step("execute", complete=True, shelved=True) == msg


def test_next_step_validates_offers_advance_and_names_next_phase():
    for phase in ("brainstorm", "spec", "plan"):
        hint = workflow.next_step(phase, validates=True)
        assert "specflo advance" in hint         # names the verb to run
        assert workflow.next_phase(phase) in hint  # names the phase it moves to
    # concretely: a validating spec offers a move to the plan phase
    assert "plan" in workflow.next_step("spec", validates=True)


def test_next_step_validates_false_or_omitted_returns_the_work_hint():
    for phase in ("brainstorm", "spec", "plan"):
        assert workflow.next_step(phase, validates=False) == workflow.next_step(phase)
        assert workflow.next_step(phase) == workflow._NEXT_STEP[phase]


def test_next_step_execute_ignores_validates():
    # execute keeps its progress-based hint; the validated branch never fires
    assert workflow.next_step("execute", validates=True) == workflow.next_step("execute")
    pending = {"total": 2, "all_done": False, "next_actionable": ["T-01"]}
    assert workflow.next_step("execute", progress=pending, validates=True) == (
        workflow.next_step("execute", progress=pending)
    )


def test_next_step_shelved_takes_precedence_over_validates():
    shelved = workflow.next_step("spec", shelved=True)
    assert workflow.next_step("spec", validates=True, shelved=True) == shelved


def test_resolve_reopen_target_bare_returns_the_immediately_previous_phase():
    assert workflow.resolve_reopen_target("spec") == "brainstorm"
    assert workflow.resolve_reopen_target("plan") == "spec"
    assert workflow.resolve_reopen_target("execute") == "plan"


def test_resolve_reopen_target_named_earlier_phase_is_returned():
    assert workflow.resolve_reopen_target("execute", "brainstorm") == "brainstorm"
    assert workflow.resolve_reopen_target("execute", "spec") == "spec"
    assert workflow.resolve_reopen_target("plan", "brainstorm") == "brainstorm"


def test_resolve_reopen_target_bare_at_first_phase_raises():
    with pytest.raises(ValueError):
        workflow.resolve_reopen_target("brainstorm")


def test_resolve_reopen_target_named_current_phase_raises():
    with pytest.raises(ValueError):
        workflow.resolve_reopen_target("spec", "spec")


def test_resolve_reopen_target_named_later_phase_raises():
    with pytest.raises(ValueError):
        workflow.resolve_reopen_target("spec", "plan")
    with pytest.raises(ValueError):
        workflow.resolve_reopen_target("brainstorm", "execute")


def test_resolve_reopen_target_unknown_names_raise():
    with pytest.raises(ValueError):
        workflow.resolve_reopen_target("spec", "nonsense")
    with pytest.raises(ValueError):
        workflow.resolve_reopen_target("nonsense")  # unknown current phase


def test_resolve_reopen_target_four_error_conditions_are_distinct():
    # bare-at-first, named-current, named-later, and unknown-target each carry a
    # distinct message so the CLI (and a human) can tell them apart.
    messages = []
    for call in (
        lambda: workflow.resolve_reopen_target("brainstorm"),          # already at first
        lambda: workflow.resolve_reopen_target("spec", "spec"),        # current phase
        lambda: workflow.resolve_reopen_target("spec", "plan"),        # later phase
        lambda: workflow.resolve_reopen_target("spec", "nonsense"),    # unknown name
    ):
        with pytest.raises(ValueError) as exc:
            call()
        messages.append(str(exc.value))
    assert len(set(messages)) == 4


def test_resolve_reopen_target_later_phase_error_points_to_advance():
    with pytest.raises(ValueError) as exc:
        workflow.resolve_reopen_target("spec", "plan")
    assert "advance" in str(exc.value).lower()


# --- the review-aware all-tasks-done hint (review-rounds REQ-20) --------------
# Once every task is done, what to do next depends entirely on where the review
# stands, so the hint splits four ways.

_ALL_DONE = {"total": 2, "all_done": True, "next_actionable": []}


def _hint(review):
    return workflow.next_step("execute", progress=_ALL_DONE, review=review)


def _closed(number, verdict):
    from specflo import review

    return {"rounds": number, "latest": number, "verdict": verdict, "open": False,
            "passing": verdict in review.PASSING,
            "date": "2026-08-02", "sha": "abc1234", "reason": "",
            "file": f"review-{number}.md"}


def test_next_step_review_hint_with_no_round_calls_for_the_review():
    hint = _hint(None)
    assert "review start" in hint
    assert "specflo advance" not in hint


def test_next_step_review_hint_with_an_open_round_names_that_file():
    open_round = {"rounds": 3, "latest": 3, "verdict": "", "open": True,
                  "passing": False, "date": "2026-08-09", "sha": "", "reason": "",
                  "file": "review-3.md"}
    hint = _hint(open_round)
    assert "review-3.md" in hint
    assert "review done" in hint
    assert "specflo advance" not in hint


def test_next_step_review_hint_with_changes_requested_sends_you_back():
    hint = _hint(_closed(2, "changes-requested"))
    assert "review-2.md" in hint
    assert "review start" in hint                  # another round, not an advance
    assert "specflo advance" not in hint


def test_next_step_review_hint_with_a_passing_round_offers_advance():
    for verdict in ("ready-to-merge", "waived"):
        hint = _hint(_closed(1, verdict))
        assert "specflo advance" in hint, verdict
        assert verdict in hint


def test_next_step_review_hints_differ_across_all_four_states():
    hints = {
        _hint(None),
        _hint({"rounds": 1, "latest": 1, "verdict": "", "open": True,
               "passing": False, "date": "2026-08-09", "sha": "", "reason": "",
               "file": "review-1.md"}),
        _hint(_closed(1, "changes-requested")),
        _hint(_closed(1, "ready-to-merge")),
    }
    assert len(hints) == 4


# --- hint and gate agree on which verdicts pass (round 1, F4) -----------------
# The hint used to branch on "not changes-requested", so a verdict the gate
# rejects still got offered `specflo advance`. One rule, one owner.


def test_next_step_offers_advance_only_for_passing_verdicts():
    from specflo import review

    for verdict in ("ready-to-merge", "changes-requested", "waived", "approved",
                    "lgtm", ""):
        state = _closed(1, verdict)
        state["open"] = not verdict
        state["passing"] = verdict in review.PASSING
        hint = _hint(state)
        assert ("specflo advance" in hint) is state["passing"], verdict


def test_review_state_marks_passing_verdicts_for_every_reader(tmp_path):
    # The hint cannot import review (review -> projects -> workflow), so the
    # judgement travels in the payload rather than being re-derived downstream.
    import reviewhelp
    from specflo import config, projects, review

    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-08-01")
    review.start_round(tmp_path, cfg, "thing", today="2026-08-01")
    assert review.review_state(tmp_path, cfg, "thing")["passing"] is False  # open

    reviewhelp.close_round(tmp_path, cfg, "thing", "ready-to-merge", today="2026-08-02")
    assert review.review_state(tmp_path, cfg, "thing")["passing"] is True


def test_the_gate_and_the_hint_never_disagree_on_a_passing_verdicts_set():
    # Stated once, structurally: both read review.PASSING rather than each
    # keeping its own idea of which verdicts clear the review.
    from specflo import review

    assert review.PASSING == ("ready-to-merge", "waived")
    assert set(review.PASSING) < set(review.VERDICTS)


# --- level-aware next steps ------------------------------------------------------

from typer.testing import CliRunner  # noqa: E402

from specflo.cli import app  # noqa: E402

_runner = CliRunner()


def test_fast_next_steps_keep_going_until_the_plan_validates():
    for phase, nxt in (("brainstorm", "spec"), ("spec", "plan")):
        hint = workflow.next_step(phase, validates=True, level="fast")
        assert "without waiting" in hint and nxt in hint
    for phase in ("spec", "plan"):
        hint = workflow.next_step(phase, level="fast")
        assert "without waiting" in hint and phase in hint


def test_fast_next_step_stops_once_for_approval_when_the_plan_validates():
    hint = workflow.next_step("plan", validates=True, level="fast")
    assert "approv" in hint and "specflo doc show brief" in hint
    assert "without waiting" not in hint


def test_full_next_steps_are_unchanged_by_the_level_argument():
    for phase in ("brainstorm", "spec", "plan", "execute"):
        for validates in (False, True):
            assert workflow.next_step(phase, validates=validates, level="full") == \
                workflow.next_step(phase, validates=validates)


def test_quick_next_step_names_the_brief():
    hint = workflow.next_step("execute", level="quick")
    assert "brief" in hint and "Proof" in hint


def _ok(args, stdin=None):
    result = _runner.invoke(app, args, input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result


def test_fast_advance_out_of_brainstorm_and_spec_says_keep_going(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _ok(["init"])
    _ok(["new", "Thing", "--level", "fast"])
    _ok(["decision", "add", "--text", "one", "--rationale", "r"])
    _ok(["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], "none\n")
    out = _ok(["advance"]).output
    assert "without waiting" in out and "spec" in out
    _ok(["spec", "start"])
    _ok(["requirement", "add", "--text", "it works", "--acceptance", "it runs"])
    _ok(["section", "set", "spec", "In scope", "--stdin"], "- it.\n")
    _ok(["section", "set", "spec", "Out of scope", "--stdin"], "- rest.\n")
    out = _ok(["advance"]).output
    assert "without waiting" in out and "plan" in out
    _ok(["plan", "start"])
    _ok(["task", "add", "--text", "build", "--acceptance", "a", "--verify", "true",
         "--from", "REQ-01"])
    status = _ok(["status"]).output
    assert "approv" in status and "without waiting" not in status


# --- the hints follow the converging review loop -------------------------------------


def test_hint_after_changes_within_budget_names_the_open_items():
    state = {**_closed(1, "changes-requested"), "open_items": ["F-01", "F-02"],
             "budget_spent": False, "after_changes": False}

    hint = _hint(state)

    assert "F-01, F-02" in hint
    assert "fix the blocker and should-fix items" in hint
    assert "never the nits" in hint
    assert "commit" in hint and "specflo review start" in hint
    assert "--over-budget" not in hint and "specflo advance" not in hint


def test_hint_at_the_budget_names_the_two_choices():
    state = {**_closed(2, "changes-requested"), "open_items": ["F-03"],
             "budget_spent": True, "after_changes": True}

    hint = _hint(state)

    assert "specflo review start --over-budget" in hint
    assert "specflo review waive --reason" in hint
    assert "specflo advance" not in hint


def test_hint_after_a_pass_that_followed_changes_asks_for_the_whole_suite():
    after = _hint({**_closed(2, "ready-to-merge"), "after_changes": True})
    first = _hint({**_closed(1, "ready-to-merge"), "after_changes": False})

    assert "whole test suite" in after and "specflo advance" in after
    assert "whole test suite" not in first and "specflo advance" in first


def _execute_all_done(tmp_path, monkeypatch):
    from test_cli import _project_at_execute

    monkeypatch.chdir(tmp_path)
    _project_at_execute(_runner, app, tmp_path)
    _runner.invoke(app, ["task", "start", "T-01"])
    _runner.invoke(app, ["task", "done", "T-01"])
    return tmp_path / "docs" / "projects" / "thing"


def _write_round(project_dir, number, verdict, findings):
    (project_dir / f"review-{number}.md").write_text(
        f"---\nround: {number}\nverdict: {verdict}\ndate: '2026-08-22'\nsha: ''\n"
        f"level: full\nreason: ''\n---\n\n# Review round {number}\n\n## Findings\n\n"
        + "\n".join(findings) + "\n"
    )


def _surfaces():
    """The next-step hint as status, the checkpoint and guide each report it."""
    import json as _json

    status = _json.loads(_runner.invoke(app, ["status", "--json"]).stdout)["next_step"]
    do_next = _runner.invoke(app, ["checkpoint"]).stdout.split("## Do next", 1)[1]
    guide = _json.loads(_runner.invoke(app, ["guide", "--json"]).stdout)["next_step"]
    return {"status": status, "checkpoint": do_next, "guide": guide}


def test_every_surface_names_the_open_items_after_changes(tmp_path, monkeypatch):
    project_dir = _execute_all_done(tmp_path, monkeypatch)
    _write_round(project_dir, 1, "changes-requested", ["- F-01 (blocker) One", "- F-02 (nit) Two"])

    for surface, hint in _surfaces().items():
        assert "(F-01)" in hint, surface
        assert "specflo review start" in hint, surface


def test_every_surface_names_the_two_choices_at_the_budget(tmp_path, monkeypatch):
    project_dir = _execute_all_done(tmp_path, monkeypatch)
    _write_round(project_dir, 1, "changes-requested", ["- F-01 (blocker) One"])
    _write_round(project_dir, 2, "changes-requested", ["- F-02 (blocker) Two"])

    for surface, hint in _surfaces().items():
        assert "specflo review start --over-budget" in hint, surface
        assert "specflo review waive --reason" in hint, surface


def test_every_surface_asks_for_the_whole_suite_after_fixes(tmp_path, monkeypatch):
    project_dir = _execute_all_done(tmp_path, monkeypatch)
    _write_round(project_dir, 1, "changes-requested", ["- F-01 (blocker) One"])
    _write_round(project_dir, 2, "ready-to-merge", ["- none"])

    for surface, hint in _surfaces().items():
        assert "whole test suite" in hint, surface
        assert "specflo advance" in hint, surface
