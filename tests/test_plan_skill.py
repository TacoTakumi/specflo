from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "plan" / "SKILL.md"


def test_skill_file_exists():
    assert SKILL.is_file()


def test_skill_has_the_anatomy_sections():
    text = SKILL.read_text().lower()
    for heading in ["when to use", "hard-gate", "process", "rationalization",
                    "red flags", "verification"]:
        assert heading in text, f"missing section: {heading}"


def test_skill_references_the_real_commands():
    text = SKILL.read_text()
    for cmd in ["specflo plan start", "specflo task add", "specflo validate plan",
                "specflo advance"]:
        assert cmd in text, f"missing command reference: {cmd}"


def test_skill_gives_thin_milestone_guidance():
    # REQ-17: the planner authors milestones with Exit checklists and assigns
    # every task to one — via the milestone commands, deferring mechanics to CLI.
    text = SKILL.read_text()
    for cmd in ["specflo milestone add", "specflo task set-milestone"]:
        assert cmd in text, f"missing milestone command reference: {cmd}"
    low = text.lower()
    assert "exit checklist" in low          # Exit checklists authored per milestone
    assert "--milestone" in text            # every task assigned to a milestone
