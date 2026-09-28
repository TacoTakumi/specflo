"""Each skill step that runs the whole suite names the configured command.

The repo's whole-suite command is the `test_command` config key. A step that
tells the agent to run the whole (or full) suite must say how to read it,
`specflo config get test_command`, and what to run when it is unset.
"""

import re
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1] / "skills"

READ_IT = "specflo config get test_command"
WHEN_UNSET = "usual full test command"

# An instruction to run the whole suite: "Run the full suite", "run the whole
# test suite". "not the whole suite" and "If a Verify runs the whole suite" are
# not instructions and do not match.
RUN_WHOLE_SUITE = re.compile(r"\b[Rr]un the (?:whole|full) (?:test )?suite\b")

# A new step starts at a blank line, a bullet or a numbered item.
STEP_START = re.compile(r"^\s*(?:$|[-*] |\d+\. )")

# The fewest whole-suite steps each skill holds today, so a rewording that hides
# a step from the pattern fails here instead of passing with nothing to check.
MIN_STEPS = {"specflo-quick": 1, "specflo-execute": 3}


def _steps(name):
    """The skill's steps (paragraphs, bullets, numbered items), each on one line."""
    steps, current = [], []
    for line in (SKILLS / name / "SKILL.md").read_text().splitlines():
        if STEP_START.match(line) and current:
            steps.append(" ".join(" ".join(current).split()))
            current = []
        if line.strip():
            current.append(line)
    if current:
        steps.append(" ".join(" ".join(current).split()))
    return steps


def _whole_suite_steps(name):
    return [step for step in _steps(name) if RUN_WHOLE_SUITE.search(step)]


@pytest.mark.parametrize("name", sorted(MIN_STEPS))
def test_the_skill_has_its_whole_suite_steps(name):
    assert len(_whole_suite_steps(name)) >= MIN_STEPS[name]


@pytest.mark.parametrize("name", sorted(MIN_STEPS))
def test_each_whole_suite_step_reads_test_command(name):
    for step in _whole_suite_steps(name):
        assert READ_IT in step, step


@pytest.mark.parametrize("name", sorted(MIN_STEPS))
def test_each_whole_suite_step_says_what_to_run_when_it_is_unset(name):
    for step in _whole_suite_steps(name):
        assert WHEN_UNSET in step, step
