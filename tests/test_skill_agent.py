"""T-16: the controller-facing agent skill stays pinned to the CLI (REQ-20).

The skill must name every shipped `specflo agent` verb and the documented
exit codes, and prescribe the background blocking-prompt pattern; these tests
fail when a verb or code disappears from either the CLI or the skill.
"""

from __future__ import annotations

import re
from pathlib import Path

import typer.main

from specflo.agent import cli as agent_cli
from specflo.agent.cli import agent_app

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "skills" / "specflo-agent" / "SKILL.md"

DOCUMENTED_CODES = {
    "0": 0,
    "10": agent_cli.EXIT_BUSY,
    "11": agent_cli.EXIT_TIMEOUT,
    "12": agent_cli.EXIT_UNREACHABLE,
    "13": agent_cli.EXIT_DETACHED,
    "1": agent_cli.EXIT_GENERIC,
}


def cli_verbs() -> set[str]:
    group = typer.main.get_command(agent_app)
    return set(group.commands)


def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def skill_verbs() -> set[str]:
    # every `specflo agent <verb>` reference in the skill body
    return set(re.findall(r"specflo agent ([a-z-]+)", skill_text()))


def test_skill_exists_in_the_repo_family():
    assert SKILL_MD.is_file()
    head = skill_text().split("---")[1]
    assert "name: specflo-agent" in head


def test_skill_names_every_shipped_verb():
    missing = cli_verbs() - skill_verbs()
    assert not missing, f"skill does not mention verbs: {sorted(missing)}"


def test_skill_names_no_phantom_verbs():
    phantom = skill_verbs() - cli_verbs()
    assert not phantom, f"skill names verbs the CLI lacks: {sorted(phantom)}"


def test_exit_code_contract_pinned_both_sides():
    # the CLI constants still carry the documented values...
    assert agent_cli.EXIT_BUSY == 10
    assert agent_cli.EXIT_TIMEOUT == 11
    assert agent_cli.EXIT_UNREACHABLE == 12
    assert agent_cli.EXIT_DETACHED == 13
    assert agent_cli.EXIT_GENERIC == 1
    # ...and the skill states each code
    text = skill_text()
    for code in DOCUMENTED_CODES:
        assert re.search(rf"`{code}`", text), f"skill does not state exit code {code}"


def test_skill_prescribes_background_blocking_prompt_pattern():
    text = skill_text().lower()
    assert "background task" in text
    assert "completion notification" in text
    assert "never busy-poll" in text
