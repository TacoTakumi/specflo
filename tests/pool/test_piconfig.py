"""A hosted member's pi configuration: the routing flags of its egress class.

A hosted member starts against a pi configuration directory generated for it
alone, and ``PI_CODING_AGENT_DIR`` points its pi there. The ``models.json`` in
it tells OpenRouter which providers may serve the member: a ``no-train`` member
is routed only to providers that collect no data and retain none, an ``open``
member is routed freely. A local member sends nothing off this host, so nothing
is generated for it. The directory lasts as long as the lease it was made for.
"""

import json
from dataclasses import replace

import pytest

from specflo.errors import SpecfloError
from specflo.pool import launch, piconfig
from specflo.pool.config import Account, Member
from specflo.pool.definitions import AgentDefinition

DEFINITION = AgentDefinition(
    name="rebaser",
    role="Rebases the work branch and reports conflicts",
    prompt="You are the rebaser.",
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

NO_TRAIN_MEMBER = Member(
    name="hosted-1",
    command="pi --mode rpc --model 'openrouter/some-vendor/some-model'",
    backing="hosted",
    labels=(),
    capacity=2,
    egress="no-train",
    model="some-vendor/some-model",
    account="team-a",
)

OPEN_MEMBER = replace(NO_TRAIN_MEMBER, name="hosted-2", egress="open")

ACCOUNTS = (
    Account(name="team-a", cap=2, key_env="TEAM_A_KEY"),
    Account(name="team-b", cap=2, key_env="TEAM_B_KEY"),
)

CALLER = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/pool",
    "PI_CODING_AGENT_DIR": "/home/pool/.pi/agent",
    "TEAM_A_KEY": "key-of-team-a",
    "TEAM_B_KEY": "key-of-team-b",
}

NO_TRAIN_ROUTING = {"data_collection": "deny", "zdr": True}


def _provider(directory):
    config = json.loads((directory / piconfig.MODELS_FILE).read_text(encoding="utf-8"))
    assert list(config) == ["providers"]
    assert list(config["providers"]) == [piconfig.PROVIDER]
    return config["providers"][piconfig.PROVIDER]


def test_a_no_train_member_is_routed_away_from_data_collection_and_retention(tmp_path):
    directory = piconfig.create(tmp_path, NO_TRAIN_MEMBER, ACCOUNTS)

    provider = _provider(directory)
    # On the provider, so the flags hold for whichever model the command picks.
    assert provider["compat"]["openRouterRouting"] == NO_TRAIN_ROUTING
    entry = provider["modelOverrides"]["some-vendor/some-model"]
    assert entry["compat"]["openRouterRouting"] == NO_TRAIN_ROUTING


def test_a_no_train_member_that_names_no_model_still_carries_the_flags(tmp_path):
    member = replace(NO_TRAIN_MEMBER, model=None)

    provider = _provider(piconfig.create(tmp_path, member, ACCOUNTS))

    assert provider["compat"]["openRouterRouting"] == NO_TRAIN_ROUTING
    assert "modelOverrides" not in provider


def test_an_open_member_has_neither_flag(tmp_path):
    directory = piconfig.create(tmp_path, OPEN_MEMBER, ACCOUNTS)

    provider = _provider(directory)
    assert "compat" not in provider
    assert "modelOverrides" not in provider
    text = (directory / piconfig.MODELS_FILE).read_text(encoding="utf-8")
    assert "data_collection" not in text
    assert "zdr" not in text
    assert "openRouterRouting" not in text


def test_a_local_member_gets_no_generated_configuration(tmp_path):
    assert piconfig.create(tmp_path, LOCAL_MEMBER, ACCOUNTS) is None
    assert list(tmp_path.iterdir()) == []

    env = launch.member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)
    assert launch.AGENT_DIR_ENV not in env


def test_the_member_is_pointed_at_its_generated_directory(tmp_path):
    directory = piconfig.create(tmp_path, NO_TRAIN_MEMBER, ACCOUNTS)

    env = launch.member_env(
        DEFINITION, NO_TRAIN_MEMBER, ACCOUNTS, CALLER, config_dir=directory
    )

    assert launch.AGENT_DIR_ENV == "PI_CODING_AGENT_DIR"
    assert env[launch.AGENT_DIR_ENV] == str(directory)
    assert directory.parent == tmp_path


def test_the_callers_pi_directory_never_stands_in_for_the_generated_one():
    # The caller's own pi configuration carries no routing flags, so a
    # definition that lists the variable must not bring it in.
    definition = replace(DEFINITION, env=("PI_CODING_AGENT_DIR",))

    env = launch.member_env(definition, NO_TRAIN_MEMBER, ACCOUNTS, CALLER)

    assert launch.AGENT_DIR_ENV not in env


def test_the_configuration_names_the_key_variable_and_never_holds_a_key(tmp_path):
    directory = piconfig.create(tmp_path, NO_TRAIN_MEMBER, ACCOUNTS)

    assert _provider(directory)["apiKey"] == "$TEAM_A_KEY"
    for path in directory.iterdir():
        text = path.read_text(encoding="utf-8")
        assert "key-of-team-a" not in text
        assert "TEAM_B_KEY" not in text
    # Nothing of the user's own pi directory is copied: no stored credentials.
    assert not (directory / "auth.json").exists()


def test_a_hosted_member_whose_account_is_not_declared_is_refused(tmp_path):
    member = replace(NO_TRAIN_MEMBER, account="team-c")

    with pytest.raises(launch.LaunchError) as excinfo:
        piconfig.create(tmp_path, member, ACCOUNTS)

    assert isinstance(excinfo.value, SpecfloError)
    assert "team-c" in str(excinfo.value)
    assert list(tmp_path.iterdir()) == []


def test_two_leases_on_one_member_get_a_directory_each(tmp_path):
    first = piconfig.create(tmp_path, NO_TRAIN_MEMBER, ACCOUNTS)
    second = piconfig.create(tmp_path, NO_TRAIN_MEMBER, ACCOUNTS)

    assert first != second
    # Ending one lease leaves the other's configuration in place.
    piconfig.remove(first)
    assert not first.exists()
    assert _provider(second)["compat"]["openRouterRouting"] == NO_TRAIN_ROUTING


def test_the_directory_is_removed_when_the_lease_ends(tmp_path):
    directory = piconfig.create(tmp_path, NO_TRAIN_MEMBER, ACCOUNTS)
    # What pi wrote there during the lease goes with it.
    (directory / "sessions").mkdir()
    (directory / "sessions" / "one.jsonl").write_text("{}\n", encoding="utf-8")

    piconfig.remove(directory)

    assert not directory.exists()
    assert list(tmp_path.iterdir()) == []
    # Ending a lease twice, or a local member's lease, removes nothing.
    piconfig.remove(directory)
    piconfig.remove(None)


def test_a_directory_that_was_not_generated_is_never_removed(tmp_path):
    own = tmp_path / "agent"
    own.mkdir()
    (own / piconfig.MODELS_FILE).write_text("{}", encoding="utf-8")

    with pytest.raises(launch.LaunchError):
        piconfig.remove(own)

    assert (own / piconfig.MODELS_FILE).exists()
