"""The specflo-research-project skill ships, is bundled, and carries the session rules.

A research project is worked in sessions, often with a domain expert at the
keyboard. These tests read the shipped skill for each rule of that session
loop (recap first, record as it lands, one question at a time, close with a
summary), check that the bundle and the skills lifecycle include it, and pin
the commands it cites that the CLI already registers.
"""

import importlib.util
import re
from pathlib import Path

from agentsquire import read_stamp
from agentsquire.sources import default_source
from click.testing import CliRunner

from specflo.cli import build_cli

NAME = "specflo-research-project"
REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / "skills" / NAME / "SKILL.md"


def _text():
    return SKILL.read_text()


def _flat(text):
    return " ".join(text.split())


def _section(heading):
    text = _text()
    assert f"\n{heading}\n" in text, f"the skill has no {heading} section"
    return _flat(text.split(f"\n{heading}\n", 1)[1].split("\n## ", 1)[0])


def _description():
    head = _text().split("\n---\n", 1)[0]
    return _flat(head.split("description: ", 1)[1])


# --- shape --------------------------------------------------------------------


def test_the_skill_ships_with_frontmatter_and_the_usual_sections():
    text = _text()
    assert text.startswith(f"---\nname: {NAME}\ndescription: Use when ")
    for heading in (
        "## When to use",
        "## When NOT to use",
        "## Process",
        "## Rationalizations",
        "## Red flags",
    ):
        assert f"\n{heading}\n" in text, heading


def test_the_description_names_triggers_and_a_do_not_use_clause():
    description = _description()
    assert "research project" in description
    assert "specflo research recap" in description
    assert "Do NOT use" in description


def test_the_skill_carries_no_numbered_record_codes():
    # Placeholders such as S-NN, Q-NN and RF-NN are fine; a real number is not.
    assert re.findall(r"\b(?:REQ|FU|T|D|M|F|S|Q|RF)-\d+\b", _text()) == []


def test_the_skill_is_pure_ascii():
    assert [ch for ch in _text() if ord(ch) > 0x7F] == []


def test_the_skill_never_calls_the_feature_deep_research():
    assert "deep research" not in _text().lower()


# --- the session rules --------------------------------------------------------


def test_every_session_starts_with_the_recap():
    text = _flat(_text())
    assert "Start every session with `specflo research recap`" in text


def test_the_usual_user_and_destination_are_recorded_once_on_the_first_session():
    text = _flat(_text())
    assert "first session" in text
    assert "in their own words" in text
    assert "specflo research user set --text <text> | --file <path>" in text
    assert (
        "specflo research destination set build|inform|learn|unknown --reason <text>"
        in text
    )
    assert "Do not ask who is at the keyboard" in text


def test_one_question_at_a_time():
    assert "one question at a time" in _flat(_text()).lower()


def test_knowledge_questions_carry_no_suggested_answer():
    text = _flat(_text())
    assert "draw out the user's knowledge" in text
    assert "no suggested answer" in text


def test_choice_questions_carry_a_recommended_answer():
    text = _flat(_text())
    assert "about a choice" in text
    assert "a recommended answer" in text


def test_the_user_is_told_early_they_can_always_ask_for_a_recommendation():
    text = _flat(_text())
    assert "Early in the session" in text
    assert "can always ask for your recommendation" in text


def test_plain_language_when_the_usual_user_is_not_a_developer():
    text = _flat(_text())
    assert "plain language" in text
    assert "not a developer" in text


def test_each_entry_is_recorded_with_the_cli_as_it_lands():
    text = _flat(_text())
    assert "as it lands" in text
    assert "Do not batch" in text
    for command in (
        "specflo research source add --kind interview|web|repo --title <text>",
        "specflo research ingest <file>",
        "specflo research finding add --text <text> --kind fact|inference "
        "--status supported|contradicted|unsure",
        "specflo research question add --text <text> --by user|developer",
        "specflo research term set --term <term> --definition <text>",
        "specflo decision add --text",
        "--supersedes",
    ):
        assert command in text, command
    assert "never deleted" in text


def test_developer_questions_are_marked_and_kept_broad():
    text = _flat(_text())
    assert "--by developer" in text
    assert "--default" in text
    assert "--why" in text
    assert "keep developer questions broad" in text
    assert "specflo research ask-dev" in text
    assert "email" in text


def test_the_outgoing_search_rule_holds_whatever_the_egress_class():
    text = _flat(_text())
    assert "Never put names, private figures or text from the project's sources" in text
    assert "outgoing web search" in text
    assert "egress suggestion" in text
    assert "whatever the egress class" in text


def test_a_wiki_save_needs_an_installed_wiki_and_the_users_agreement():
    text = _flat(_text())
    assert "only when one is installed and the user agrees" in text
    assert "The CLI never writes to a wiki" in text


def test_every_session_ends_with_session_close():
    text = _flat(_text())
    assert (
        "End every session with `specflo research session close "
        "--summary <text> --next <text> [--present <who>]`" in text
    )


def test_the_skill_names_the_ways_out_of_research():
    text = _flat(_text())
    assert "specflo research spinoff <name> --level quick|fast|full --use <ID>" in text
    assert "specflo level fast|full" in text
    assert "specflo advance" in text


# --- the cited commands that exist are registered -----------------------------


def _resolve(words):
    command = build_cli()
    while words and words[0] in (getattr(command, "commands", None) or {}):
        command, words = command.commands[words[0]], words[1:]
    return command, words


def _citation_problem(span):
    """Why `specflo <span>` is not a registered command and its flags, or None."""
    command, rest = _resolve(span.split())
    if getattr(command, "commands", None):
        return f"`specflo {span}` names no command"
    opts = {opt for param in command.params for opt in getattr(param, "opts", ())}
    flags = set(re.findall(r"(?<![\w-])(--[a-z][\w-]*)", " ".join(rest)))
    if not flags <= opts:
        return f"`specflo {span}` cites unknown flags {sorted(flags - opts)}"
    return None


def test_every_registered_command_and_flag_the_skill_cites_is_right():
    # The research group may land in another change; until the CLI registers
    # it, its spans are skipped rather than failing on an absent verb.
    groups = build_cli().commands
    spans = re.findall(r"`specflo ([^`]+)`", _text())
    assert spans, "the skill cites no specflo command"
    checked = [s for s in spans if s.split()[0] in groups]
    assert checked, "the skill cites no registered specflo command"
    problems = [p for p in map(_citation_problem, checked) if p]
    assert problems == [], "\n".join(problems)


def test_the_citation_check_catches_a_wrong_verb_or_flag():
    assert _citation_problem("decision add --text x --rationale y") is None
    assert _citation_problem("level fast|full") is None
    assert _citation_problem("decision add --reason y")
    assert _citation_problem("decision")


# --- bundled and installed ----------------------------------------------------


def _hatch_build():
    spec = importlib.util.spec_from_file_location(
        "hatch_build", REPO_ROOT / "hatch_build.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_skill_is_in_the_bundle_and_the_build_guard():
    assert NAME in _hatch_build().EXPECTED_SKILLS
    assert NAME in [s.name for s in default_source("specflo").list_skills()]


def _run(*args):
    return CliRunner().invoke(build_cli(), list(args), catch_exceptions=False)


def test_skills_install_places_it_and_status_lists_it(fixture_roots):
    home, _ = fixture_roots
    installed = _run("skills", "install")
    assert installed.exit_code == 0, installed.output
    skill_md = home / ".claude" / "skills" / NAME / "SKILL.md"
    assert skill_md.is_file()
    stamp = read_stamp(skill_md.read_text())
    assert stamp is not None and stamp["source_package"] == "specflo"
    status = _run("skills", "status")
    assert status.exit_code == 0
    assert f"up-to-date {NAME}" in status.output
