"""A member's pi command line: the harness command plus the definition's flags.

A definition reaches pi only as startup flags: its tools, its skills and its
prompt body. Nothing else in it is put on the command line or into the
environment, and the project's context files are kept out unless the
definition asks for them.
"""

import os
from dataclasses import replace

from specflo.pool import launch
from specflo.pool.config import Member
from specflo.pool.definitions import AgentDefinition

PROMPT = "You are the rebaser.\n\nNever push. Report each conflict with its file."

DEFINITION = AgentDefinition(
    name="rebaser",
    role="Rebases the work branch and reports conflicts",
    prompt=PROMPT,
    tools=("read", "bash"),
    skills=("model-update-check",),
    deny=("git push", "rm -rf"),
    env=("GIT_AUTHOR_NAME",),
    credentials=("github-read",),
    needs=("strong-model",),
    egress="open",
)

MEMBER = Member(
    name="local-1",
    command="pi --mode rpc --model 'llama-swap/tc3'",
    backing="local",
    labels=("strong-model",),
    capacity=1,
    egress="local",
    model="tc3",
)

HARNESS = ["pi", "--mode", "rpc", "--model", "llama-swap/tc3"]


def test_the_argv_is_the_harness_command_plus_the_definition_flags():
    argv = launch.pi_argv(DEFINITION, MEMBER)

    assert argv == [
        *HARNESS,
        "-e", launch.DENY_EXTENSION,
        "--tools", "read,bash",
        "--skill", "model-update-check",
        "--append-system-prompt", PROMPT,
        "--no-context-files",
    ]


def test_each_skill_gets_its_own_flag():
    definition = replace(DEFINITION, skills=("model-update-check", "landscape-scan"))

    argv = launch.pi_argv(definition, MEMBER)

    assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--skill"] == [
        "model-update-check", "landscape-scan",
    ]


def test_project_context_requested_keeps_the_context_files():
    definition = replace(DEFINITION, project_context=True)

    argv = launch.pi_argv(definition, MEMBER)

    assert "--no-context-files" not in argv
    assert argv == [a for a in launch.pi_argv(DEFINITION, MEMBER) if a != "--no-context-files"]


def test_a_definition_with_no_tools_turns_every_tool_off():
    # Leaving --tools out would hand the member pi's default tools.
    argv = launch.pi_argv(replace(DEFINITION, tools=()), MEMBER)

    assert "--no-tools" in argv
    assert "--tools" not in argv


def test_no_other_definition_content_reaches_the_argv_or_the_environment():
    before = dict(os.environ)

    argv = launch.pi_argv(DEFINITION, MEMBER)

    flags = argv[len(HARNESS):]
    withheld = (
        DEFINITION.name, DEFINITION.role, DEFINITION.egress,
        *DEFINITION.deny, *DEFINITION.env, *DEFINITION.credentials, *DEFINITION.needs,
    )
    for value in withheld:
        assert not any(value in arg for arg in flags if arg != PROMPT), value
    assert dict(os.environ) == before
