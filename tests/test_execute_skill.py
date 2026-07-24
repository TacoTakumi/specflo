from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "specflo-execute" / "SKILL.md"


def test_skill_file_exists():
    assert SKILL.is_file()


def test_skill_has_the_anatomy_sections():
    text = SKILL.read_text().lower()
    for heading in ["when to use", "hard-gate", "process", "rationalization",
                    "red flags", "verification"]:
        assert heading in text, f"missing section: {heading}"


def test_skill_references_the_real_commands():
    text = SKILL.read_text()
    for cmd in ["specflo task show", "specflo task start", "specflo task done",
                "specflo validate execute", "specflo advance", "specflo checkpoint"]:
        assert cmd in text, f"missing command reference: {cmd}"


def test_skill_gives_thin_milestone_guidance():
    # REQ-17: the executor honours the soft boundary verify beat and the
    # working-ahead label, deferring the mechanics to the milestone commands.
    text = SKILL.read_text()
    assert "specflo milestone" in text      # reference the commands, don't reimplement
    low = text.lower()
    assert "boundary" in low                # the soft milestone-boundary verify beat
    assert "exit checklist" in low          # surfaced/verified at the boundary
    assert "working ahead" in low or "working-ahead" in low
