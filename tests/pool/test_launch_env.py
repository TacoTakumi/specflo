"""A member's environment: a fixed baseline plus what its definition lists.

What a member is given is the only hard limit on it, so nothing reaches it
from the caller's environment by default. A variable gets through when it is
on the baseline, when the definition lists it, or when it holds the API key of
the hosted member's own account.
"""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from specflo.errors import SpecfloError
from specflo.pool import launch
from specflo.pool.config import Account, Member
from specflo.pool.definitions import AgentDefinition

DEFINITION = AgentDefinition(
    name="rebaser",
    role="Rebases the work branch and reports conflicts",
    prompt="You are the rebaser.",
    env=("GIT_AUTHOR_NAME",),
    credentials=("GH_READ_TOKEN",),
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

ACCOUNTS = (
    Account(name="team-a", cap=2, key_env="TEAM_A_KEY"),
    Account(name="team-b", cap=2, key_env="TEAM_B_KEY"),
)

CALLER = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/pool",
    "LANG": "C.UTF-8",
    "TERM": "xterm-256color",
    "TMPDIR": "/var/tmp",
    "SECRET_X": "not-for-members",
    "GIT_AUTHOR_NAME": "Pool Rebaser",
    "GH_READ_TOKEN": "read-only-token",
    "TEAM_A_KEY": "key-of-team-a",
    "TEAM_B_KEY": "key-of-team-b",
}



# Every member starts with a pi configuration directory generated for its
# lease, so the builder is asked for one here as the runner asks for one.
CONFIG_DIR = Path("/generated/pi-config")


def member_env(definition, member, accounts, environ, **fields):
    """``launch.member_env`` with the generated directory every member has."""
    fields.setdefault("config_dir", CONFIG_DIR)
    return launch.member_env(definition, member, accounts, environ, **fields)


def test_a_variable_the_definition_does_not_list_is_withheld():
    env = member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert "SECRET_X" not in env


def test_the_variables_and_credentials_the_definition_lists_are_present():
    env = member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert env["GIT_AUTHOR_NAME"] == CALLER["GIT_AUTHOR_NAME"]
    assert env["GH_READ_TOKEN"] == CALLER["GH_READ_TOKEN"]


def test_the_baseline_is_a_fixed_list_that_lets_pi_start():
    # pi is a node program found on PATH; it keeps its settings under HOME.
    assert launch.BASELINE_ENV == (
        "PATH", "HOME", "USER", "LOGNAME", "SHELL",
        "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    )


def test_the_environment_is_the_baseline_plus_what_is_listed_and_nothing_else():
    env = member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert env == {
        "PATH": CALLER["PATH"],
        "HOME": CALLER["HOME"],
        "LANG": CALLER["LANG"],
        "TERM": CALLER["TERM"],
        "GIT_AUTHOR_NAME": CALLER["GIT_AUTHOR_NAME"],
        "GH_READ_TOKEN": CALLER["GH_READ_TOKEN"],
        # What the pool says about the member itself, never the caller.
        launch.SERVE_ENV: "0",
        launch.AGENT_NAME_ENV: LOCAL_MEMBER.name,
        launch.AGENT_MANAGED_ENV: "1",
        launch.AGENT_DIR_ENV: str(CONFIG_DIR),
        launch.TMPDIR_ENV: "/tmp",
    }


def test_a_name_the_caller_does_not_have_is_left_out_not_set_empty():
    caller = {k: v for k, v in CALLER.items() if k not in ("LANG", "GIT_AUTHOR_NAME")}

    env = member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, caller)

    assert "LANG" not in env
    assert "GIT_AUTHOR_NAME" not in env


def test_the_temp_directory_is_the_sandbox_s_own_whatever_the_caller_or_definition_says():
    definition = replace(DEFINITION, env=(*DEFINITION.env, "TMPDIR"))

    env = member_env(definition, LOCAL_MEMBER, ACCOUNTS, {**CALLER, "TMPDIR": "/var/tmp"})

    assert env["TMPDIR"] == "/tmp"


def test_a_hosted_member_gets_the_key_of_its_own_account_and_no_other():
    env = member_env(DEFINITION, HOSTED_MEMBER, ACCOUNTS, CALLER)

    assert env["TEAM_A_KEY"] == CALLER["TEAM_A_KEY"]
    assert "TEAM_B_KEY" not in env


def test_a_local_member_gets_no_account_key():
    env = member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert not {a.key_env for a in ACCOUNTS} & set(env)


def test_listing_another_accounts_key_variable_does_not_hand_it_over():
    # The cap on an account holds only if no member outside it has its key.
    definition = replace(DEFINITION, env=("GIT_AUTHOR_NAME", "TEAM_B_KEY"))

    hosted = member_env(definition, HOSTED_MEMBER, ACCOUNTS, CALLER)
    local = member_env(definition, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert "TEAM_B_KEY" not in hosted
    assert "TEAM_A_KEY" in hosted
    assert "TEAM_B_KEY" not in local


def test_a_hosted_member_whose_key_is_not_set_is_refused_by_name_only():
    caller = {k: v for k, v in CALLER.items() if k != "TEAM_A_KEY"}

    with pytest.raises(SpecfloError) as refused:
        member_env(DEFINITION, HOSTED_MEMBER, ACCOUNTS, caller)

    message = str(refused.value)
    assert "hosted-1" in message and "team-a" in message and "TEAM_A_KEY" in message
    assert not any(value in message for value in caller.values())


def test_a_hosted_member_whose_account_is_not_declared_is_refused():
    member = replace(HOSTED_MEMBER, account="team-c")

    with pytest.raises(SpecfloError) as refused:
        member_env(DEFINITION, member, ACCOUNTS, CALLER)

    message = str(refused.value)
    assert "hosted-1" in message and "team-c" in message
    assert not any(value in message for value in CALLER.values())


def test_building_the_environment_changes_neither_the_caller_nor_the_process():
    caller = dict(CALLER)
    before = dict(os.environ)

    env = member_env(DEFINITION, HOSTED_MEMBER, ACCOUNTS, caller)
    env["ADDED_LATER"] = "1"

    assert caller == CALLER
    assert dict(os.environ) == before


def test_a_member_serves_no_control_surface_of_its_own():
    # pi discovers the specflo control extension wherever a developer has
    # installed it, a member's pi included. Without this the extension binds
    # a second control socket for the member, keyed by the working
    # directory's name, which the agent host's lease wall does not guard.
    env = member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert env.get(launch.SERVE_ENV) == "0"


def test_a_member_carries_the_agent_handshake_of_its_own_name():
    env = member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert env.get(launch.AGENT_NAME_ENV) == LOCAL_MEMBER.name
    assert env.get(launch.AGENT_MANAGED_ENV) == "1"


def test_neither_the_caller_nor_the_definition_sets_the_handshake():
    # The three are the pool's to say, like the deny list: a definition that
    # lists one, or a caller that exports one, changes nothing.
    definition = replace(
        DEFINITION,
        env=("GIT_AUTHOR_NAME", launch.SERVE_ENV, launch.AGENT_MANAGED_ENV),
        credentials=("GH_READ_TOKEN", launch.AGENT_NAME_ENV),
    )
    caller = dict(
        CALLER,
        **{
            launch.SERVE_ENV: "1",
            launch.AGENT_NAME_ENV: "not-the-member",
            launch.AGENT_MANAGED_ENV: "0",
        },
    )

    env = member_env(definition, LOCAL_MEMBER, ACCOUNTS, caller)

    assert env[launch.SERVE_ENV] == "0"
    assert env.get(launch.AGENT_NAME_ENV) == LOCAL_MEMBER.name
    assert env.get(launch.AGENT_MANAGED_ENV) == "1"


def test_the_git_global_variable_names_the_generated_file_when_there_is_one(tmp_path):
    (tmp_path / launch.GITCONFIG_FILE).write_text("[user]\n\tname = Op\n", encoding="utf-8")

    env = member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER, config_dir=tmp_path)

    assert env[launch.GIT_CONFIG_GLOBAL_ENV] == str(tmp_path / launch.GITCONFIG_FILE)


def test_with_no_generated_git_file_the_git_global_variable_is_not_set():
    env = member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert launch.GIT_CONFIG_GLOBAL_ENV not in env


@pytest.mark.parametrize("listed", ["env", "credentials"])
def test_neither_the_caller_nor_the_definition_sets_the_git_global_variable(tmp_path, listed):
    definition = replace(DEFINITION, **{listed: (launch.GIT_CONFIG_GLOBAL_ENV,)})
    caller = {**CALLER, launch.GIT_CONFIG_GLOBAL_ENV: "/home/pool/.gitconfig"}
    with_file = tmp_path / "with"
    with_file.mkdir()
    (with_file / launch.GITCONFIG_FILE).write_text("", encoding="utf-8")
    without_file = tmp_path / "without"
    without_file.mkdir()

    given = member_env(definition, LOCAL_MEMBER, ACCOUNTS, caller, config_dir=with_file)
    none = member_env(definition, LOCAL_MEMBER, ACCOUNTS, caller, config_dir=without_file)

    assert given[launch.GIT_CONFIG_GLOBAL_ENV] == str(with_file / launch.GITCONFIG_FILE)
    assert launch.GIT_CONFIG_GLOBAL_ENV not in none
