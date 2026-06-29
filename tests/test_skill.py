from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "brainstorm" / "SKILL.md"


def test_skill_file_exists():
    assert SKILL.is_file()


def test_skill_has_the_anatomy_sections():
    text = SKILL.read_text()
    for heading in [
        "When to use",
        "HARD-GATE",
        "Process",
        "rationalization",  # case-insensitive match below
        "Red flags",
        "Verification",
    ]:
        assert heading.lower() in text.lower(), f"missing section: {heading}"


def test_skill_references_the_real_commands():
    text = SKILL.read_text()
    for cmd in [
        "specflo brainstorm start",
        "specflo decision add",
        "specflo validate brainstorm",
    ]:
        assert cmd in text, f"missing command reference: {cmd}"


def test_brainstorm_skill_preflight_reflects_new_scaffolds():
    """REQ-06: the preflight states `new` scaffolds brainstorm.md and frames
    `brainstorm start` as locate/resume, not the create step."""
    text = SKILL.read_text()
    lower = text.lower()
    assert "scaffolded by `specflo new`" in text  # `new` named as the scaffolder
    assert "specflo brainstorm start" in text  # still referenced
    assert "locate" in lower  # framed as locate/resume


def test_brainstorm_skill_weaves_in_research():
    text = SKILL.read_text()
    lower = text.lower()
    assert "skills/research" in text, "brainstorm skill must reference the research skill path"
    for phrase in ["landscape scan", "opportunistic", "research subagent"]:
        assert phrase in lower, f"missing research seam phrase: {phrase}"
