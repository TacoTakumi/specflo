"""A member's environment: a fixed baseline plus what its definition lists.

What a member is given is the only hard limit on it, so nothing reaches it
from the caller's environment by default. A variable gets through when it is
on the baseline, when the definition lists it, or when it holds the API key of
the hosted member's own account.
"""

import os
from dataclasses import replace

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


def test_a_variable_the_definition_does_not_list_is_withheld():
    env = launch.member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert "SECRET_X" not in env


def test_the_variables_and_credentials_the_definition_lists_are_present():
    env = launch.member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert env["GIT_AUTHOR_NAME"] == CALLER["GIT_AUTHOR_NAME"]
    assert env["GH_READ_TOKEN"] == CALLER["GH_READ_TOKEN"]


def test_the_baseline_is_a_fixed_list_that_lets_pi_start():
    # pi is a node program found on PATH; it keeps its settings under HOME.
    assert launch.BASELINE_ENV == (
        "PATH", "HOME", "USER", "LOGNAME", "SHELL",
        "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "TMPDIR",
    )


def test_the_environment_is_the_baseline_plus_what_is_listed_and_nothing_else():
    env = launch.member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert env == {
        "PATH": CALLER["PATH"],
        "HOME": CALLER["HOME"],
        "LANG": CALLER["LANG"],
        "TERM": CALLER["TERM"],
        "TMPDIR": CALLER["TMPDIR"],
        "GIT_AUTHOR_NAME": CALLER["GIT_AUTHOR_NAME"],
        "GH_READ_TOKEN": CALLER["GH_READ_TOKEN"],
    }


def test_a_name_the_caller_does_not_have_is_left_out_not_set_empty():
    caller = {k: v for k, v in CALLER.items() if k not in ("TMPDIR", "GIT_AUTHOR_NAME")}

    env = launch.member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, caller)

    assert "TMPDIR" not in env
    assert "GIT_AUTHOR_NAME" not in env


def test_a_hosted_member_gets_the_key_of_its_own_account_and_no_other():
    env = launch.member_env(DEFINITION, HOSTED_MEMBER, ACCOUNTS, CALLER)

    assert env["TEAM_A_KEY"] == CALLER["TEAM_A_KEY"]
    assert "TEAM_B_KEY" not in env


def test_a_local_member_gets_no_account_key():
    env = launch.member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert not {a.key_env for a in ACCOUNTS} & set(env)


def test_listing_another_accounts_key_variable_does_not_hand_it_over():
    # The cap on an account holds only if no member outside it has its key.
    definition = replace(DEFINITION, env=("GIT_AUTHOR_NAME", "TEAM_B_KEY"))

    hosted = launch.member_env(definition, HOSTED_MEMBER, ACCOUNTS, CALLER)
    local = launch.member_env(definition, LOCAL_MEMBER, ACCOUNTS, CALLER)

    assert "TEAM_B_KEY" not in hosted
    assert "TEAM_A_KEY" in hosted
    assert "TEAM_B_KEY" not in local


def test_a_hosted_member_whose_key_is_not_set_is_refused_by_name_only():
    caller = {k: v for k, v in CALLER.items() if k != "TEAM_A_KEY"}

    with pytest.raises(SpecfloError) as refused:
        launch.member_env(DEFINITION, HOSTED_MEMBER, ACCOUNTS, caller)

    message = str(refused.value)
    assert "hosted-1" in message and "team-a" in message and "TEAM_A_KEY" in message
    assert not any(value in message for value in caller.values())


def test_a_hosted_member_whose_account_is_not_declared_is_refused():
    member = replace(HOSTED_MEMBER, account="team-c")

    with pytest.raises(SpecfloError) as refused:
        launch.member_env(DEFINITION, member, ACCOUNTS, CALLER)

    message = str(refused.value)
    assert "hosted-1" in message and "team-c" in message
    assert not any(value in message for value in CALLER.values())


def test_building_the_environment_changes_neither_the_caller_nor_the_process():
    caller = dict(CALLER)
    before = dict(os.environ)

    env = launch.member_env(DEFINITION, HOSTED_MEMBER, ACCOUNTS, caller)
    env["ADDED_LATER"] = "1"

    assert caller == CALLER
    assert dict(os.environ) == before
