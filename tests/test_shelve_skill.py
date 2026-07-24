from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "specflo-shelve" / "SKILL.md"


def test_skill_file_exists():
    assert SKILL.is_file()


def test_skill_documents_both_commands():
    text = SKILL.read_text()
    assert "specflo shelve" in text
    assert "specflo resume" in text


def test_skill_documents_the_stop_and_resume_intents():
    low = SKILL.read_text().lower()
    for stop in ["we're done with this", "drop this", "not worth it", "shelve it"]:
        assert stop in low, f"missing stop-intent phrase: {stop}"
    assert "pick that back up" in low  # resume-intent


def test_skill_captures_an_optional_reason():
    assert "--reason" in SKILL.read_text()


def test_skill_description_triggers_on_intent():
    # the YAML frontmatter description carries the trigger phrases, so the harness
    # can route stop/resume intent here without the user naming the command.
    text = SKILL.read_text()
    assert text.startswith("---")
    description = text.split("---", 2)[1].lower()
    assert "shelve it" in description or "not worth it" in description  # stop-intent
    assert "pick that back up" in description                          # resume-intent
