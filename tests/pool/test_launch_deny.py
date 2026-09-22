"""A member's command deny list: one pi extension, loaded on every member.

The extension rides on every member's command line with ``-e``, whatever its
definition denies, and the definition's deny list reaches it in one
environment variable. The list never goes on the command line. The extension
is a guard against mistakes, not a security boundary: what a member is given
stays the only hard limit on it.
"""

import json
import os
from dataclasses import replace
from pathlib import Path

from specflo.pool import launch
from specflo.pool.config import Account, Member
from specflo.pool.definitions import AgentDefinition

DEFINITION = AgentDefinition(
    name="rebaser",
    role="Rebases the work branch and reports conflicts",
    prompt="You are the rebaser.",
    tools=("read", "bash"),
    deny=("git push", "rm -rf"),
)

LOCAL_MEMBER = Member(
    name="local-1",
    command="pi --mode rpc --model 'llama-swap/tc3'",
    backing="local",
    labels=(),
    capacity=1,
    egress="local",
    model="tc3",
)

HOSTED_MEMBER = Member(
    name="hosted-1",
    command="pi --mode rpc --model 'openrouter/some-model'",
    backing="hosted",
    labels=(),
    capacity=1,
    egress="no-train",
    account="team-a",
)

MEMBERS = (LOCAL_MEMBER, HOSTED_MEMBER)

ACCOUNTS = (Account(name="team-a", cap=2, key_env="TEAM_A_KEY"),)

CALLER = {"PATH": "/usr/bin:/bin", "HOME": "/home/pool", "TEAM_A_KEY": "key-of-team-a"}


def _extension_paths(argv):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]



# Every member starts with a pi configuration directory generated for its
# lease, so the builder is asked for one here as the runner asks for one.
CONFIG_DIR = Path("/generated/pi-config")


def member_env(definition, member, accounts, environ, **fields):
    """``launch.member_env`` with the generated directory every member has."""
    fields.setdefault("config_dir", CONFIG_DIR)
    return launch.member_env(definition, member, accounts, environ, **fields)


def test_the_extension_is_a_file_shipped_inside_the_package():
    path = Path(launch.DENY_EXTENSION)

    assert path.is_absolute()
    assert path.is_file()
    assert path == Path(launch.__file__).parent / "pi_extension" / "deny.ts"


def test_every_members_argv_loads_the_extension_once():
    for member in MEMBERS:
        argv = launch.pi_argv(DEFINITION, member)

        assert _extension_paths(argv) == [launch.DENY_EXTENSION], member.name


def test_a_definition_that_denies_nothing_still_loads_the_extension():
    argv = launch.pi_argv(replace(DEFINITION, deny=()), LOCAL_MEMBER)

    assert _extension_paths(argv) == [launch.DENY_EXTENSION]


def test_the_deny_list_reaches_the_extension_in_one_variable():
    for member in MEMBERS:
        env = member_env(DEFINITION, member, ACCOUNTS, CALLER)

        assert json.loads(env[launch.DENY_ENV]) == ["git push", "rm -rf"], member.name


def test_the_deny_list_is_not_put_on_the_command_line():
    argv = launch.pi_argv(DEFINITION, LOCAL_MEMBER)

    for rule in DEFINITION.deny:
        assert not any(rule in arg for arg in argv), rule


def test_a_definition_that_denies_nothing_sets_no_variable():
    env = member_env(replace(DEFINITION, deny=()), LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert launch.DENY_ENV not in env


def test_the_callers_own_deny_variable_never_reaches_a_member():
    # The list comes from the definition alone, even when the definition
    # asks for the variable by name.
    caller = {**CALLER, launch.DENY_ENV: json.dumps(["ls"])}
    asking = replace(DEFINITION, env=(launch.DENY_ENV,))

    denying = member_env(asking, LOCAL_MEMBER, ACCOUNTS, caller)
    open_ = member_env(replace(asking, deny=()), LOCAL_MEMBER, ACCOUNTS, caller)

    assert json.loads(denying[launch.DENY_ENV]) == ["git push", "rm -rf"]
    assert launch.DENY_ENV not in open_


def test_building_the_argv_and_the_environment_changes_nothing_in_the_process():
    before = dict(os.environ)

    launch.pi_argv(DEFINITION, LOCAL_MEMBER)
    member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert dict(os.environ) == before


def test_the_extension_says_it_is_a_guard_and_not_a_security_boundary():
    header = Path(launch.DENY_EXTENSION).read_text().split("*/", 1)[0]

    assert "guard against mistakes" in header
    assert "not a security boundary" in header
