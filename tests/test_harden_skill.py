"""The specflo-harden skill ships, doctor checks it, and it covers hardening.

A harden-level project is worked from a brief that names what to harden, in
review rounds whose findings become fix tasks, until the user stops. These
tests read the shipped skill for each step of that loop, pin the verbs and
flags it cites to the ones the CLI registers, and run `specflo doctor` over
each harness to show it reports the skill as it does the others.
"""

import json
import re
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentsquire.harnesses import CLAUDE_CODE, HERMES, OPENCODE, PI, default_registry
from agentsquire.sources import default_source

from specflo import doctor
from specflo.brief import HARDEN_SECTIONS
from specflo.cli import build_cli
from specflo.projects import NEW_LEVELS

NAME = "specflo-harden"
SKILL = Path(__file__).resolve().parents[1] / "skills" / NAME / "SKILL.md"

# Every harness agentsquire knows, so doctor is shown to report the skill in
# each. A harness added upstream fails the completeness check below instead of
# going unchecked.
BACKENDS = {b.name: b for b in (CLAUDE_CODE, PI, HERMES, OPENCODE)}


def _text():
    return SKILL.read_text()


def _flat(text):
    return " ".join(text.split())


def _section(heading):
    text = _text()
    assert f"\n{heading}\n" in text, f"the skill has no {heading} section"
    return _flat(text.split(f"\n{heading}\n", 1)[1].split("\n## ", 1)[0])


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


def test_hardening_is_attended_so_the_skill_keeps_auto_and_level_out():
    section = _section("## When NOT to use")
    assert "specflo auto" in section
    assert "specflo level" in section


def test_the_skill_carries_no_numbered_record_codes():
    # Placeholders such as F-NN are fine; a real record number is not.
    assert re.findall(r"\b(?:REQ|FU|T|D|M|F)-\d+\b", _text()) == []


def test_the_skill_is_pure_ascii():
    assert [ch for ch in _text() if ord(ch) > 0x7F] == []


# --- the loop -----------------------------------------------------------------


def test_the_skill_starts_a_harden_level_project():
    assert "harden" in NEW_LEVELS
    assert "specflo new <name> --level harden" in _text()


@pytest.mark.parametrize("title", HARDEN_SECTIONS)
def test_the_skill_fills_each_brief_section_through_the_cli(title):
    assert f'specflo section set brief "{title}" --stdin' in _text()


def test_the_skill_names_the_three_brief_sections_and_validates_the_brief():
    assert tuple(HARDEN_SECTIONS) == ("Scope", "Focus", "Stop when")
    assert "specflo validate brief" in _text()


def test_the_skill_runs_harden_rounds_with_a_fresh_reviewer():
    text = _flat(_text())
    for command in (
        "specflo review start --harden",
        "specflo review prompt",
        "specflo review finding add --severity blocker|should-fix|nit --at",
        "specflo review finding check F-NN closed|open",
        "specflo review done",
    ):
        assert command in text, command
    assert "fresh-context" in text


def test_the_skill_turns_each_blocking_finding_into_a_fix_task():
    text = _flat(_text())
    assert "specflo task add --fixes F-NN" in text
    assert "every path" in text
    assert "red then green" in text
    assert "never the nits" in text


def test_the_skill_suggests_the_stop_after_two_quiet_rounds_and_leaves_it_to_the_user():
    text = _flat(_text())
    assert "two harden rounds in a row" in text
    assert "no new" in text
    assert "the user's call" in text


# The same reading of a whole-suite step as the test-command test uses.
RUN_WHOLE_SUITE = re.compile(r"\b[Rr]un the (?:whole|full) (?:test )?suite\b")
STEP_START = re.compile(r"^\s*(?:$|[-*] |\d+\. )")


def _whole_suite_steps():
    steps, current = [], []
    for line in _text().splitlines():
        if STEP_START.match(line) and current:
            steps.append(_flat(" ".join(current)))
            current = []
        if line.strip():
            current.append(line)
    if current:
        steps.append(_flat(" ".join(current)))
    return [step for step in steps if RUN_WHOLE_SUITE.search(step)]


def test_the_skill_runs_the_whole_suite_once_at_the_stop():
    steps = _whole_suite_steps()
    assert any("once" in step and "stop" in step for step in steps), steps
    for step in steps:
        assert "specflo config get test_command" in step, step
        assert "usual full test command" in step, step


def test_the_skill_completes_through_validate_and_advance():
    text = _text()
    assert "specflo validate execute" in text
    assert "specflo advance" in text


# --- the cited commands exist -------------------------------------------------


def _resolve(words):
    """The command a `specflo ...` span names, and the words after it.

    Groups are walked by their ``commands`` mapping: Typer builds its own
    command classes, so they are not instances of the click package's Group.
    """
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


def test_every_command_and_flag_the_skill_cites_is_registered():
    spans = re.findall(r"`specflo ([^`]+)`", _text())
    assert spans, "the skill cites no specflo command"
    problems = [p for p in map(_citation_problem, spans) if p]
    assert problems == [], "\n".join(problems)


def test_the_citation_check_catches_a_wrong_verb_or_flag():
    # Guard the check itself so a green run means something.
    assert _citation_problem("review start --harden") is None
    assert _citation_problem("new <name> --level harden") is None
    assert _citation_problem("review start --hardened")
    assert _citation_problem("review finding")
    assert _citation_problem("task add --fix F-NN")


# --- doctor checks it in every harness -----------------------------------------


def test_the_harness_list_is_every_harness_agentsquire_knows():
    assert set(BACKENDS) == set(default_registry().names())


def _cli(*args):
    return CliRunner().invoke(build_cli(), list(args), catch_exceptions=False)


def _harness_check(name):
    result = _cli("doctor", "--json")
    checks = {c["subject"]: c for c in json.loads(result.stdout)["checks"]}
    return checks[f"{name} (user)"]


@pytest.mark.parametrize("name", sorted(BACKENDS))
def test_doctor_reports_the_harden_skill_in_each_harness(name, tmp_path, monkeypatch):
    backend = BACKENDS[name]
    home, project = tmp_path / "home", tmp_path / "project"
    (home / backend.user_marker_dirs[0]).mkdir(parents=True)
    project.mkdir()
    monkeypatch.setenv("AGENTSQUIRE_HOME", str(home))
    monkeypatch.setenv("AGENTSQUIRE_PROJECT", str(project))
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/local/bin/specflo")

    names = [s.name for s in default_source("specflo").list_skills()]
    assert NAME in names

    installed = _cli("skills", "install", "--harness", f"{name}:user")
    assert installed.exit_code == 0, installed.output
    check = _harness_check(name)
    assert check["status"] == "ok", check
    assert check["detail"] == f"{len(names)} of {len(names)} skills installed"

    # Doctor counts the skill as one of the set: without it the harness fails.
    shutil.rmtree(backend.skills_dir("user", home=home, project=project) / NAME)
    check = _harness_check(name)
    assert check["status"] == "fail", check
    assert check["detail"] == f"missing: {NAME}"
    assert f"`specflo skills install --harness {name}:user`" in check["fix"]
