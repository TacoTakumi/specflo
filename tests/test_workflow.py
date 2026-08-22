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
    from specflo import config, projects, review

    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-08-01")
    review.start_round(tmp_path, cfg, "thing", today="2026-08-01")
    assert review.review_state(tmp_path, cfg, "thing")["passing"] is False  # open

    review.close_round(tmp_path, cfg, "thing", "ready-to-merge", today="2026-08-02")
    assert review.review_state(tmp_path, cfg, "thing")["passing"] is True


def test_the_gate_and_the_hint_never_disagree_on_a_passing_verdicts_set():
    # Stated once, structurally: both read review.PASSING rather than each
    # keeping its own idea of which verdicts clear the review.
    from specflo import review

    assert review.PASSING == ("ready-to-merge", "waived")
    assert set(review.PASSING) < set(review.VERDICTS)
