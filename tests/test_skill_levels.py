"""The shipped skills carry the text each level needs."""

from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / "skills"


def _skill(name):
    return (SKILLS / name / "SKILL.md").read_text()


def test_the_quick_skill_ships_with_its_rules():
    text = _skill("specflo-quick")
    assert text.startswith("---\nname: specflo-quick\n")
    for phrase in (
        "section set brief",
        "specflo validate brief",
        "Proof",
        "One commit",
        "no review",
        "specflo level fast",
    ):
        assert phrase in text, phrase
