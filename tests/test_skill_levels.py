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


def _fast_section(name):
    text = _skill(name)
    assert "## Fast level" in text, name
    section = text.split("## Fast level", 1)[1].split("\n## ", 1)[0]
    return " ".join(section.split())


def test_the_phase_skills_carry_a_fast_level_section():
    for name in ("specflo-brainstorm", "specflo-spec", "specflo-plan"):
        section = _fast_section(name)
        for phrase in (
            "You make the decisions",
            "approaches weighed",
            "Do not interview",
            "do not pause",
            "Out of scope / Deferred",
            "One approval, before execute",
            "have not seen",
        ):
            assert phrase in section, (name, phrase)


def test_the_execute_skill_stops_on_uncovered_work_at_light_levels():
    text = _skill("specflo-execute")
    assert "## Levels" in text
    section = " ".join(text.split("## Levels", 1)[1].split("\n## ", 1)[0].split())
    for phrase in (
        "Uncovered work stops the loop",
        "specflo level fast",
        "specflo level full",
        "Do not add tasks",
    ):
        assert phrase in section, phrase



def test_the_execute_and_quick_skills_defer_uncovered_work_in_a_ladder():
    for name in ("specflo-execute", "specflo-quick"):
        text = " ".join(_skill(name).split())
        assert "In a ladder run" in text or "in a ladder run" in text, name
        assert "Deferred" in text, name


def _levels_section(name):
    text = _skill(name)
    return " ".join(text.split("## Levels", 1)[1].split("\n## ", 1)[0].split())


def test_the_light_levels_run_targeted_tests_and_the_full_suite_once():
    plan = _fast_section("specflo-plan")
    execute = _levels_section("specflo-execute")
    quick = " ".join(_skill("specflo-quick").split())
    for name, text in (("plan", plan), ("execute", execute), ("quick", quick)):
        assert "not the whole suite" in text, name
        assert "full suite once" in text, name
    assert "Targeted Verify" in plan
    assert "task edit T-NN --verify" in execute
    assert "before the commit" in quick
