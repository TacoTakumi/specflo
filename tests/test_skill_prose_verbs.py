"""The phase skills read artifacts with `doc show` and write prose with `section set`.

An agent following the brainstorm, spec, or plan skill never opens an artifact
file itself: the CLI is the only path to a project's documents, so the same
skill text serves a project in the checkout and one held by a daemon. These
tests scan the shipped skills for both halves of that rule.
"""

import re
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1] / "skills"
PHASE_SKILLS = ["specflo-brainstorm", "specflo-spec", "specflo-plan"]
ARTIFACT_FILES = ("brainstorm.md", "spec.md", "plan.md")

# A line that names an artifact file and tells the agent to change it by hand.
_EDIT_VERB = re.compile(r"\b(edit|editing|hand-edit|rewrite|rewriting|write to|open)\b", re.I)
# A line that forbids the direct edit is the rule itself, not a violation.
_NEGATED = re.compile(r"\b(never|do not|don't|not|no)\b", re.I)


def _skill_text(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text()


def _direct_edit_instructions(text: str) -> list[str]:
    hits = []
    for line in text.splitlines():
        if not any(f"`{name}`" in line or name in line for name in ARTIFACT_FILES):
            continue
        if _EDIT_VERB.search(line) and not _NEGATED.search(line):
            hits.append(line.strip())
    return hits


@pytest.mark.parametrize("name", PHASE_SKILLS)
def test_phase_skill_reads_artifacts_with_doc_show(name):
    assert "specflo doc show" in _skill_text(name), f"{name} never names doc show"


@pytest.mark.parametrize("name", PHASE_SKILLS)
def test_phase_skill_writes_prose_with_section_set(name):
    assert "specflo section set" in _skill_text(name), f"{name} never names section set"


@pytest.mark.parametrize("name", PHASE_SKILLS)
def test_phase_skill_never_instructs_a_direct_artifact_edit(name):
    hits = _direct_edit_instructions(_skill_text(name))
    assert hits == [], f"{name} tells the agent to edit an artifact file directly:\n" + "\n".join(hits)


@pytest.mark.parametrize("name", PHASE_SKILLS)
def test_phase_skill_does_not_say_the_command_prints_a_path(name):
    # Start commands print a locator; a skill that expects a path misleads the agent.
    text = _skill_text(name).lower()
    assert "prints its path" not in text
    assert "prints the path" not in text


def test_scan_catches_a_direct_edit_and_lets_the_rule_through():
    # Guard the scanner itself so a green run means something.
    assert _direct_edit_instructions("Keep it current by editing `spec.md` directly.")
    assert _direct_edit_instructions("Rewrite `brainstorm.md` yourself when it drifts.")
    assert not _direct_edit_instructions("Never hand-edit `plan.md` - the CLI owns it.")
    assert not _direct_edit_instructions("Read `spec.md` with `specflo doc show spec`.")
