"""A member's pi command line: the harness command plus the definition's flags.

A definition reaches pi as startup flags for its tools and its prompt body.
Nothing else in it is put on the command line or into the environment, and the
project's context files are kept out unless the definition asks for them.

Its skills are the exception, and they are not here at all: ``--skill`` is a
path with no resolver anywhere in pi, so each declared skill is copied into
the member's generated configuration directory, where pi discovers it by name.
"""

import os
from dataclasses import replace

from specflo.pool import cli_admin, definitions, launch
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
        "--append-system-prompt", PROMPT,
        "--no-context-files",
    ]


def test_no_skill_a_definition_names_reaches_the_command_line():
    definition = replace(DEFINITION, skills=("model-update-check", "landscape-scan"))

    argv = launch.pi_argv(definition, MEMBER)

    assert "--skill" not in argv
    assert not any("skill" in arg for arg in argv if arg != PROMPT)


def test_no_shipped_definition_puts_a_skill_on_the_command_line():
    shipped = [
        definitions.load_definition(path)
        for path in sorted(cli_admin.SHIPPED_DIR.glob("*.md"))
    ]

    assert any(definition.skills for definition in shipped)
    for definition in shipped:
        argv = launch.pi_argv(definition, MEMBER)
        assert "--skill" not in argv, definition.name
        for skill in definition.skills:
            assert skill not in argv, definition.name


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


def test_extension_discovery_is_left_on():
    # A member needs the extensions a developer has installed. The one the
    # pool must silence is the control extension, and it is silenced through
    # the environment, not by turning discovery off for everything.
    argv = launch.pi_argv(DEFINITION, MEMBER)

    assert "-ne" not in argv
    assert "--no-extensions" not in argv
