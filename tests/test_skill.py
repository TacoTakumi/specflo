import re
from pathlib import Path

import typer

from specflo.cli import gate_app

SKILL = Path(__file__).resolve().parents[1] / "skills" / "specflo-brainstorm" / "SKILL.md"


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
    assert "skills/specflo-research" in text, "brainstorm skill must reference the research skill path"
    for phrase in ["landscape scan", "opportunistic", "research subagent"]:
        assert phrase in lower, f"missing research seam phrase: {phrase}"


def test_brainstorm_skill_warns_against_research_agent_type():
    """The dispatch must steer to general-purpose, not the non-existent agent type."""
    text = SKILL.read_text()
    assert "general-purpose" in text
    assert "subagent_type: research" in text  # named as the thing NOT to do


# --- requester mode ------------------------------------------------------------


def requester_mode_section():
    text = SKILL.read_text()
    match = re.search(r"^## Requester mode\n(.*?)(?=^## )", text, re.S | re.M)
    assert match, "the brainstorm skill has no Requester mode section"
    return match.group(1)


def test_requester_mode_covers_each_point_of_the_requester_conversation():
    lower = requester_mode_section().lower()
    for phrase in [
        "plain language",
        "what and why, not how",
        "one scan, early",
        "plain words",
        "decisions in plain words",
        "when the requester says they",
        "flip back on takeover",
        "developer",
        "relay the take",
    ]:
        assert phrase in lower, f"requester mode does not cover: {phrase}"


def test_requester_mode_is_entered_from_the_opening_prompt_and_left_on_the_takeover_message():
    section = requester_mode_section()
    assert "opening prompt" in section and "requester" in section
    assert "taken the gate" in section and "leave requester mode" in section


def gate_verbs():
    """The gate verbs the CLI registers, with their option names."""
    group = typer.main.get_command(gate_app)
    return {
        name: {opt for param in command.params for opt in getattr(param, "opts", ())}
        for name, command in group.commands.items()
    }


def cited_gate_verbs(section):
    """Every ``specflo gate <verb> [--flags]`` the section cites, verb to flags."""
    cited = {}
    for verb, rest in re.findall(r"`specflo gate (\w+)([^`]*)`", section):
        cited.setdefault(verb, set()).update(re.findall(r"(--[\w-]+)", rest))
    return cited


def test_the_gate_verbs_the_section_cites_are_the_ones_the_cli_registers():
    """A drift-pin: renaming gate open, gate take, or take's --by in the CLI fails here."""
    registered = gate_verbs()
    cited = cited_gate_verbs(requester_mode_section())
    assert set(cited) == {"open", "take"}, cited
    for verb, flags in cited.items():
        assert verb in registered, f"the skill cites `specflo gate {verb}`, which the CLI does not register"
        assert flags <= registered[verb], f"`specflo gate {verb}` has no {sorted(flags - registered[verb])}"
    assert "--note" in cited["open"] and "--by" in cited["take"]
