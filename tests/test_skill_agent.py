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


# -- leased members of a daemon's pool --------------------------------------
#
# An orchestrator on a hosted checkout takes a pooled member with the lease
# verbs and drives it with the agent verbs. The skill is where it learns the
# order, what a full pool does to a request, and what to do when a lease ends
# under it, so each of those is pinned to the CLI here.


def lease_cli_verbs() -> set[str]:
    from specflo.cli import lease_app

    return set(typer.main.get_command(lease_app).commands)


def skill_lease_verbs() -> set[str]:
    return set(re.findall(r"specflo lease ([a-z-]+)", skill_text()))


def lease_section() -> str:
    text = skill_text()
    assert "## Leased pool members" in text, "missing '## Leased pool members' section"
    body = text.split("## Leased pool members", 1)[1].split("\n## ", 1)[0]
    return " ".join(body.split())


def test_skill_names_every_lease_verb_and_no_phantom():
    assert lease_cli_verbs() == {"request", "release", "list"}
    assert skill_lease_verbs() == lease_cli_verbs()


def test_skill_has_no_renew_verb_and_says_renewal_is_implicit():
    # A lease is renewed by what its holder does and by a turn that runs; an
    # orchestrator told to renew would look for a verb that must never exist.
    assert "renew" not in skill_lease_verbs()
    assert "renew" not in skill_verbs()
    low = lease_section().lower()
    assert "no renew verb" in low
    assert "idle limit" in low and "expires" in low


def test_skill_says_the_lease_verbs_need_a_daemon():
    low = lease_section().lower()
    assert "hosted" in low and "daemon" in low
    assert "local" in low and "refused" in low


def test_skill_documents_request_release_and_list():
    section = lease_section()
    low = section.lower()
    assert "`specflo lease request <pool>" in section
    for option in ("--cwd", "--idle-limit", "--label", "--wait", "--egress", "--remote", "--json"):
        assert option in section, f"lease request option not documented: {option}"
    assert "lease id" in low and "agent name" in low
    assert "token file" in low
    assert "`specflo agent prompt <agent>" in section
    assert "`specflo lease release <lease>" in section
    assert "`specflo lease list [--json]`" in section
    assert "another orchestrator" in low


def test_skill_says_how_a_request_waits():
    section = lease_section()
    low = section.lower()
    assert "600" in section               # the default bound, in seconds
    assert "stderr" in low
    assert "`--wait 0`" in section
    assert "interrupt" in low and "cancel" in low
    assert "background task" in low       # a request can block like a prompt does


def test_skill_says_which_requests_are_refused_at_once():
    low = lease_section().lower()
    assert "no-train" in low
    assert "stricter" in low
    assert "refused at once" in low
    assert "closed account" in low and "reopen" in low


def test_skill_documents_the_team_form():
    section = lease_section()
    low = section.lower()
    assert "`specflo lease request --team <name>`" in section
    assert "all or nothing" in low
    assert "holds nothing while it waits" in low
    assert "one team lease id" in low
    assert "`specflo lease release <team lease id>`" in section
    assert "every member" in low
    # one member lease of a team is not given back by itself
    assert "non-zero" in low and "names the team lease id" in low
    # the members cannot reach each other; the orchestrator carries what they share
    assert "leads the team" in low
    assert "no way to message each other" in low


def test_skill_documents_reset_on_a_leased_member():
    low = lease_section().lower()
    assert "`specflo agent reset <agent>`" in lease_section()
    assert "same lease" in low
    assert "context" in low


def test_skill_documents_the_lease_ended_errors():
    from specflo.agent import lease

    section = lease_section()
    low = section.lower()
    # the three causes, worded as the CLI words them, on the unreachable code
    records = [
        {"cause": "released"},
        {"cause": "expired"},
        {"cause": "preempted", "request_id": "<request id>"},
    ]
    assert [record["cause"] for record in records] == list(lease.ENDED_CAUSES)
    for record in records:
        message = f"Error: {lease.ended_message(record)}"
        assert f"`{message}`" in section, f"skill does not state: {message}"
    assert "`12`" in section
    # what the orchestrator does next
    assert "request a lease again" in low
    assert "redo" in low and "prompt" in low
    assert "never retry" in low and "preempted" in low
    # no token or the wrong token: a generic error that shows nothing of the member
    assert "`1`" in section
    assert "leased to another holder" in low
    assert "--lease-token" in section
    assert lease.ENV_LEASE_TOKEN in section
