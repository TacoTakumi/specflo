"""A hosted member's pi configuration: the routing flags of its egress class.

A hosted member starts against a pi configuration directory generated for it
alone, and ``PI_CODING_AGENT_DIR`` points its pi there. The ``models.json`` in
it tells OpenRouter which providers may serve the member: a ``no-train`` member
is routed only to providers that collect no data and retain none, an ``open``
member is routed freely.

A local member gets a directory of its own too. It sends nothing off this
host, so it carries no routing; it gets one because the sandbox hides the
operator's configuration from every member, and a member with no directory of
its own would start with nothing at all. The directory lasts as long as the
lease it was made for.
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

# The operator's own models file, which a local member's generated one copies
# its provider from.
OPERATOR_MODELS = {
    "providers": {
        "llama-swap": {
            "baseUrl": "http://127.0.0.1:8080/v1",
            "api": "openai-completions",
            "apiKey": "a-key-the-rig-accepts",
            "models": [{"id": "tc3", "contextWindow": 32768, "maxTokens": 4096}],
        }
    }
}


def operator_models(tmp_path):
    """The operator's models file, written where a test can name it."""
    path = tmp_path / "operator-models.json"
    path.write_text(json.dumps(OPERATOR_MODELS), encoding="utf-8")
    return path


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


def test_a_local_member_gets_a_directory_of_its_own(tmp_path):
    directory = piconfig.create(
        tmp_path / "generated", LOCAL_MEMBER, ACCOUNTS, operator_models(tmp_path)
    )

    assert directory is not None
    assert directory.is_dir()
    assert directory.parent == tmp_path / "generated"
    assert (directory / piconfig.MARKER).is_file()


def test_a_local_member_carries_no_routing(tmp_path):
    directory = piconfig.create(
        tmp_path / "generated", LOCAL_MEMBER, ACCOUNTS, operator_models(tmp_path)
    )

    text = (directory / piconfig.MODELS_FILE).read_text(encoding="utf-8")
    assert "openRouterRouting" not in text
    assert piconfig.PROVIDER not in text


def test_a_member_of_any_backing_is_pointed_at_its_own_directory(tmp_path):
    for member in (LOCAL_MEMBER, NO_TRAIN_MEMBER):
        directory = piconfig.create(
            tmp_path / "generated", member, ACCOUNTS, operator_models(tmp_path)
        )
        env = launch.member_env(
            DEFINITION, member, ACCOUNTS, CALLER, config_dir=directory
        )
        assert env[launch.AGENT_DIR_ENV] == str(directory)


def test_a_member_is_never_started_with_the_variable_unset(tmp_path):
    with pytest.raises(launch.LaunchError, match=launch.AGENT_DIR_ENV):
        launch.member_env(DEFINITION, LOCAL_MEMBER, ACCOUNTS, CALLER)


def test_the_member_is_pointed_at_its_generated_directory(tmp_path):
    directory = piconfig.create(tmp_path, NO_TRAIN_MEMBER, ACCOUNTS)

    env = launch.member_env(
        DEFINITION, NO_TRAIN_MEMBER, ACCOUNTS, CALLER, config_dir=directory
    )

    assert launch.AGENT_DIR_ENV == "PI_CODING_AGENT_DIR"
    assert env[launch.AGENT_DIR_ENV] == str(directory)
    assert directory.parent == tmp_path


def test_the_callers_pi_directory_never_stands_in_for_the_generated_one(tmp_path):
    # The caller's own pi configuration is the one the sandbox hides, so a
    # definition that lists the variable must not bring it in.
    definition = replace(DEFINITION, env=("PI_CODING_AGENT_DIR",))
    directory = piconfig.create(tmp_path, NO_TRAIN_MEMBER, ACCOUNTS)

    env = launch.member_env(
        definition, NO_TRAIN_MEMBER, ACCOUNTS, CALLER, config_dir=directory
    )

    assert env[launch.AGENT_DIR_ENV] == str(directory)
    assert env[launch.AGENT_DIR_ENV] != CALLER["PI_CODING_AGENT_DIR"]


def test_the_configuration_names_the_key_variable_and_never_holds_a_key(tmp_path):
    directory = piconfig.create(tmp_path, NO_TRAIN_MEMBER, ACCOUNTS)

    assert _provider(directory)["apiKey"] == "$TEAM_A_KEY"
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
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


def test_the_operator_s_fd_and_rg_are_copied_as_files_of_the_member_s_own(tmp_path):
    tools = tmp_path / "operator-bin"
    tools.mkdir()
    (tools / "real-fd").write_text("#!/bin/sh\necho fd\n", encoding="utf-8")
    (tools / "real-fd").chmod(0o755)
    (tools / "fd").symlink_to(tools / "real-fd")
    (tools / "rg").write_text("#!/bin/sh\necho rg\n", encoding="utf-8")
    (tools / "rg").chmod(0o755)
    (tools / "other").write_text("not a tool pi fetches", encoding="utf-8")

    directory = piconfig.create(
        tmp_path / "generated", OPEN_MEMBER, ACCOUNTS, tools_from=tools
    )

    copied = directory / piconfig.TOOLS_DIR
    assert sorted(p.name for p in copied.iterdir()) == ["fd", "rg"]
    for name in ("fd", "rg"):
        assert not (copied / name).is_symlink()
        assert (copied / name).stat().st_mode & 0o100
    assert (copied / "fd").read_text(encoding="utf-8") == "#!/bin/sh\necho fd\n"


def test_no_tool_is_made_where_the_operator_has_none(tmp_path):
    directory = piconfig.create(
        tmp_path / "generated", OPEN_MEMBER, ACCOUNTS, tools_from=tmp_path / "absent"
    )

    assert not (directory / piconfig.TOOLS_DIR).exists()


def test_the_operator_s_tools_are_in_the_bin_of_the_agent_directory():
    assert piconfig.operator_tools(CALLER) == piconfig.Path("/home/pool/.pi/agent/bin")
    assert piconfig.operator_tools({"HOME": "/home/pool"}) == piconfig.Path(
        "/home/pool/.pi/agent/bin"
    )
