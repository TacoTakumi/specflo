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


RULE_OF_THUMB = ("one goal and one check", "3 to 7 tasks", "more than that")


def test_the_guide_carries_the_level_rule_of_thumb():
    from typer.testing import CliRunner
    from specflo.cli import app
    out = CliRunner().invoke(app, ["guide"]).output
    for phrase in RULE_OF_THUMB:
        assert phrase in out, phrase
    assert "confirm" in out


def test_the_brainstorm_skill_carries_the_rule_of_thumb_and_the_full_review():
    text = _skill("specflo-brainstorm")
    for phrase in RULE_OF_THUMB:
        assert phrase in text, phrase
    assert "have the user confirm" in text
    assert "specflo level full" in text
    assert "decision add --supersedes" in text
