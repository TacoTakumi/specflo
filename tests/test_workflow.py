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
