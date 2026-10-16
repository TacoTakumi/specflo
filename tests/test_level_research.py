"""Research level: one phase, research, in which a user builds up sources,
findings, questions and a glossary over sessions and nothing is built.

The level stands apart from the quick-fast-full ladder as harden does: `new`
starts a project at it, the ladder never climbs to it. Its phase is known to
the workflow, terminal, and has its own skill in the continuation."""

import pytest

from specflo import continuation, projects, workflow

LADDER_AND_HARDEN = ("quick", "fast", "full", "harden")


# --- the level --------------------------------------------------------------


def test_research_is_a_level_new_accepts_and_the_ladder_leaves_out():
    assert projects.RESEARCH_LEVEL == "research"
    assert projects.RESEARCH_LEVEL in projects.NEW_LEVELS
    assert projects.RESEARCH_LEVEL not in projects.LEVELS


def test_the_ladder_is_unchanged_by_the_research_level():
    assert projects.LEVELS == ("quick", "fast", "full")
    assert projects.NEW_LEVELS[: len(projects.LEVELS)] == projects.LEVELS


def test_validate_level_takes_research_among_the_new_levels_only():
    assert projects.validate_level("research", projects.NEW_LEVELS) == "research"
    with pytest.raises(projects.SpecfloError, match="Unknown level 'research'"):
        projects.validate_level("research")


# --- the phase --------------------------------------------------------------


def test_research_has_the_one_phase_research():
    assert workflow.RESEARCH_PHASE == "research"
    assert workflow.phases_for("research") == ["research"]


def test_no_other_level_has_the_research_phase():
    assert "research" not in workflow.PHASES
    for level in LADDER_AND_HARDEN:
        assert "research" not in workflow.phases_for(level), level


def test_the_research_phase_is_known_and_terminal():
    assert "research" in workflow.KNOWN_PHASES
    assert workflow.next_phase("research") is None


def test_the_ladder_phases_keep_their_order_and_end():
    assert list(workflow.KNOWN_PHASES[: len(workflow.PHASES)]) == workflow.PHASES
    assert workflow.next_phase("execute") is None


def test_an_unknown_phase_is_still_refused():
    with pytest.raises(ValueError, match="Unknown phase 'nonsense'"):
        workflow.next_phase("nonsense")
    with pytest.raises(ValueError, match="Unknown phase 'nonsense'"):
        workflow.next_step("nonsense")


def test_reopen_has_nowhere_earlier_to_go_from_research():
    with pytest.raises(ValueError, match="first phase 'research'"):
        workflow.resolve_reopen_target("research", level="research")
    with pytest.raises(ValueError, match="no 'brainstorm' phase"):
        workflow.resolve_reopen_target("research", "brainstorm", level="research")


# --- the next step ----------------------------------------------------------


def test_the_research_next_step_starts_the_session_with_the_recap():
    hint = workflow.next_step("research")

    assert "`specflo research recap`" in hint
    assert hint not in {workflow.next_step(phase) for phase in workflow.PHASES}


def test_the_research_next_step_is_the_same_at_its_own_level_and_when_it_validates():
    hint = workflow.next_step("research")

    assert workflow.next_step("research", level="research") == hint
    # The document validating early in the research does not end it: the
    # hint keeps the session loop rather than offering the move.
    assert workflow.next_step("research", level="research", validates=True) == hint


def test_a_complete_research_project_reads_as_complete():
    hint = workflow.next_step("research", level="research", complete=True)

    assert hint == workflow.next_step("execute", complete=True)
    assert "specflo research recap" not in hint


def test_a_shelved_research_project_reads_as_shelved():
    hint = workflow.next_step("research", level="research", shelved=True)

    assert hint == workflow.next_step("execute", shelved=True)


# --- the continuation -------------------------------------------------------


def test_the_research_phase_has_its_own_skill_in_the_continuation():
    # The default would be the research subagent skill, which scans a landscape
    # for a brainstorm; the research project skill runs a research session.
    assert continuation.PHASE_SKILLS["research"] == "specflo-research-project"
    assert continuation.phase_skill("research") == "specflo-research-project"
    assert continuation.phase_skill("research") != "specflo-research"


def test_the_research_continuation_names_the_skill_and_the_next_step():
    hint = workflow.next_step("research", level="research")

    text = continuation.build_continuation("research", hint, offer_auto=False)

    assert "Current phase: research." in text
    assert hint in text
    assert "specflo-research-project" in text
    assert continuation.CLEAR_POINT_MARKER in text


def test_the_four_ladder_skills_are_unchanged():
    for phase in workflow.PHASES:
        assert continuation.PHASE_SKILLS[phase] == f"specflo-{phase}"
