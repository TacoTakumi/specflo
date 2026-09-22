"""What a started member really loads: its own skills, and nothing else's.

A definition's skills are copied into the member's generated configuration
directory, and the sandbox hides every other place pi would look: the
operator's own configuration directory and the home-rooted skills directory
that pi resolves from HOME whatever the configuration variable says. This
reads the set back from a real pi, started the way the pool starts a member,
through a probe extension that writes the system prompt out and leaves at
``session_start`` - before pi has built a single provider request, so the run
reaches no model, neither the rig's llama-swap nor a hosted provider.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from specflo.pool import launch, piconfig, sandbox
from specflo.pool.config import Account, Member
from specflo.pool.definitions import AgentDefinition

PI = shutil.which("pi")

# The name the probe writes under, in the working directory of the lease,
# which is the one place a member may write.
PROMPT_FILE = "system-prompt.txt"

# Writes the system prompt pi built and leaves before the first request to
# any provider. The skill block of that prompt is the loaded set.
PROBE = """import { writeFileSync } from "node:fs";

export default function (pi) {
  pi.on("session_start", (event, ctx) => {
    writeFileSync("%s", ctx.getSystemPrompt(), "utf8");
    process.exit(0);
  });
}
""" % PROMPT_FILE

SKILL = """---
name: {name}
description: {description}
---

Do the {name} thing.
"""

ACCOUNTS = (Account(name="team-a", cap=2, key_env="TEAM_A_KEY"),)

DEFINITION = AgentDefinition(
    name="rebaser",
    role="Rebases the work branch",
    prompt="You are the rebaser.",
    tools=("read",),
    skills=("declared",),
)


def skill(directory: Path, name: str, description: str) -> None:
    (directory / name).mkdir(parents=True)
    (directory / name / "SKILL.md").write_text(
        SKILL.format(name=name, description=description), encoding="utf-8"
    )


def loaded_skills(prompt: str) -> set[str]:
    """Every skill named in the prompt's skill block; none without one."""
    block = re.search(r"<available_skills>(.*?)</available_skills>", prompt, re.S)
    return set(re.findall(r"<name>([^<]+)</name>", block.group(1))) if block else set()


@pytest.fixture(scope="module")
def prompt(tmp_path_factory) -> str:
    """The system prompt of a member started as the pool starts one.

    The operator's home is a temporary one laid out like the real one: a pi
    configuration directory holding two skills, and the home-rooted skills
    directory beside it holding a third. The member's definition names one
    of the three.
    """
    if PI is None:
        pytest.skip("pi is not on this rig's PATH")
    reason = sandbox.unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")

    root = tmp_path_factory.mktemp("member")
    home = root / "home"
    operator = home / ".pi" / "agent" / "skills"
    skill(operator, "declared", "The skill the definition names.")
    skill(operator, "undeclared", "A skill the operator has and no definition names.")
    skill(home / ".agents" / "skills", "home-rooted", "A skill pi finds from the home.")
    work = root / "work"
    work.mkdir()
    probe = work / "probe.js"
    probe.write_text(PROBE, encoding="utf-8")

    environ = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "TEAM_A_KEY": "no request is ever made with this",
    }
    member = Member(
        name="hosted-1",
        command=f"{PI} --offline --no-approve -e {probe}",
        backing="hosted",
        labels=(),
        capacity=1,
        egress="no-train",
        model="some-vendor/some-model",
        account="team-a",
    )
    config_dir = piconfig.create(
        root / "piconfig", member, ACCOUNTS,
        skills=DEFINITION.skills, skills_from=piconfig.operator_skills(environ),
    )
    done = subprocess.run(
        launch.member_argv(
            DEFINITION, member, environ,
            cwd=work, state_dir=root / "state", config_dir=config_dir,
        ),
        env=launch.member_env(DEFINITION, member, ACCOUNTS, environ, config_dir=config_dir),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
    )
    written = work / PROMPT_FILE
    if not written.is_file():
        raise AssertionError(
            f"the member wrote no prompt (exit {done.returncode}): {done.stderr[-2000:]}"
        )
    return written.read_text(encoding="utf-8")


def test_a_member_loads_the_skills_its_definition_names(prompt: str) -> None:
    assert "declared" in loaded_skills(prompt)


def test_it_loads_no_skill_its_definition_does_not_name(prompt: str) -> None:
    assert loaded_skills(prompt) == set(DEFINITION.skills)


def test_a_skill_of_the_operators_own_configuration_is_not_among_them(prompt: str) -> None:
    assert "undeclared" not in prompt


def test_a_skill_pi_reads_from_the_home_is_not_among_them(prompt: str) -> None:
    # The configuration variable does not close this one: pi resolves it from
    # HOME, and only the sandbox hiding that directory keeps it out.
    assert "home-rooted" not in prompt
