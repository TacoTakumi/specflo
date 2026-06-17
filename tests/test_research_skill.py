from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "research" / "SKILL.md"


def test_research_skill_exists():
    assert SKILL.is_file()


def test_research_skill_has_anatomy():
    text = SKILL.read_text().lower()
    for heading in [
        "when to use",
        "least privilege",
        "process",
        "digest contract",
        "anti-pattern",
        "red flags",
        "verification",
    ]:
        assert heading in text, f"missing section: {heading}"


def test_research_skill_declares_allowed_tools():
    assert "allowed-tools:" in SKILL.read_text()


def test_research_skill_references_the_real_tools():
    text = SKILL.read_text()
    for ref in ["awiki-search", "awiki-save", "tavily", "find-docs"]:
        assert ref in text, f"missing tool reference: {ref}"


def test_research_skill_digest_has_five_fields():
    text = SKILL.read_text().lower()
    for field in ["findings", "surprises", "sources", "freshness", "provenance"]:
        assert field in text, f"missing digest field: {field}"
