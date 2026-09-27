"""The skills, README and CHANGELOG teach the followup verbs.

A new piece of work starts by checking what earlier projects left behind, and
work found outside a project's scope is recorded rather than dropped.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _skill(name):
    return (REPO / "skills" / name / "SKILL.md").read_text()


def _step(text, label):
    """The numbered step whose bold label starts with ``label``."""
    match = re.search(rf"^\d+\. \*\*{label}.*?(?=^\d+\. \*\*|^## )", text, re.M | re.S)
    assert match, f"no step labelled {label!r}"
    return match.group(0)


def _section(text, heading):
    """The body under ``heading`` up to the next heading of the same level."""
    level = heading.split(" ", 1)[0]
    body = text.split(f"\n{heading}\n", 1)[1]
    return re.split(rf"^{level} ", body, maxsplit=1, flags=re.M)[0]


def test_the_brainstorm_skill_lists_followups_while_it_scouts():
    assert "specflo followup list" in _step(_skill("specflo-brainstorm"), "Scout")


def test_the_quick_skill_lists_followups_while_it_scouts():
    assert "specflo followup list" in _step(_skill("specflo-quick"), "Scout")


def test_the_execute_skill_records_leftover_work_as_a_followup():
    assert "specflo followup add" in _skill("specflo-execute")


def _flat(text):
    return " ".join(text.split())


def test_the_quick_skill_sends_work_outside_the_goal_to_a_followup():
    growing = _section(_skill("specflo-quick"), "## When the work grows")
    assert "specflo followup add" in growing


def test_the_quick_skill_turns_leftover_deferred_items_into_followups_on_completion():
    complete = _flat(_step(_skill("specflo-quick"), "Complete"))
    assert "Deferred" in complete and "specflo followup add" in complete


def test_the_execute_skill_turns_leftover_deferred_items_into_followups_on_completion():
    section = _flat(_section(_skill("specflo-execute"), "## Follow-ups"))
    assert "deferred list" in section
    assert "Before `specflo advance` completes the project" in section


def test_the_readme_command_reference_lists_the_followup_group():
    reference = _section((REPO / "README.md").read_text(), "## Command reference")
    for verb in ("add", "close", "list"):
        assert f"specflo followup {verb}" in reference, verb


def test_the_changelog_adds_the_followup_group_in_the_unreleased_section():
    unreleased = _section((REPO / "CHANGELOG.md").read_text(), "## [0.15.0]")
    added = _section(unreleased, "### Added")
    assert "specflo followup" in added
