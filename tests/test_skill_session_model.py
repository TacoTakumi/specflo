"""Skills dispatch subagents on the session's model.

A subagent runs on the model of the session that dispatches it. The agent uses
another model only when the user names one. A skill line that says to pick the
"most capable" model let a pi session on a local model start a hosted model for
its reviewer, so no shipped skill may tell the agent to choose a model by
capability, and each dispatch in the execute, harden and brainstorm skills
states the session-model rule.
"""

import re
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1] / "skills"

RULE = "on the session's model"
USER_NAMES = "only when the user names one"

# Wording that tells the agent to choose a model by how capable it is.
CAPABILITY = re.compile(
    r"\b(most|more|best|strongest|stronger|cheapest|cheaper|least powerful)\s+"
    r"(capable\s+|powerful\s+|available\s+)*model\b",
    re.IGNORECASE,
)


def _flat(path: Path) -> str:
    return " ".join(path.read_text().split())


def test_no_skill_picks_a_model_by_capability():
    hits = [
        f"{p.relative_to(SKILLS)}: {m.group(0)}"
        for p in sorted(SKILLS.glob("*/SKILL.md"))
        for m in CAPABILITY.finditer(_flat(p))
    ]
    assert hits == []


@pytest.mark.parametrize(
    "skill, count",
    [
        ("specflo-execute", 2),  # the reviewer and the fan-out task subagents
        ("specflo-harden", 1),  # the round's reviewer
        ("specflo-brainstorm", 1),  # the research subagent
    ],
)
def test_each_dispatch_states_the_session_model_rule(skill, count):
    text = _flat(SKILLS / skill / "SKILL.md")
    assert text.count(RULE) == count
    assert text.count(USER_NAMES) == count
