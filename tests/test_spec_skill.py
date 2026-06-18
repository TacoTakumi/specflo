from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "spec" / "SKILL.md"


def test_skill_file_exists():
    assert SKILL.is_file()


def test_skill_has_the_anatomy_sections():
    text = SKILL.read_text().lower()
    for heading in [
        "when to use",
        "hard-gate",
        "process",
        "rationalization",
        "red flags",
        "verification",
    ]:
        assert heading in text, f"missing section: {heading}"


def test_skill_references_the_real_commands():
    text = SKILL.read_text()
    for cmd in [
        "specflo spec start",
        "specflo requirement add",
        "specflo validate spec",
    ]:
        assert cmd in text, f"missing command reference: {cmd}"
