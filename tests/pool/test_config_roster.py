"""The pool configuration file: provider accounts and the member roster.

An admin declares every account and every member ahead of time; nothing that
is not declared can be leased. A roster that cannot be trusted as written is
refused with an error naming the entry and the field at fault.
"""

import copy
import dataclasses
import shutil
from pathlib import Path

import pytest
import yaml

from specflo.agent import statefiles
from specflo.errors import SpecfloError
from specflo.pool import config

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "pool"
FIXTURE = FIXTURES / "pool.yaml"

ACCOUNT = {"name": "openrouter-main", "cap": 4, "key_env": "OPENROUTER_API_KEY"}
LOCAL = {
    "name": "coder-a",
    "command": "pi --mode rpc --model model-a",
    "backing": "local",
    "model": "model-a",
    "labels": ["code"],
    "capacity": 1,
    "egress": "local",
}
HOSTED = {
    "name": "strong-hosted",
    "command": "pi --mode rpc --model vendor/strong",
    "backing": "hosted",
    "account": "openrouter-main",
    "labels": ["strong-model"],
    "capacity": 2,
    "egress": "no-train",
}


def _write(tmp_path, accounts=(ACCOUNT,), members=(LOCAL, HOSTED), **top):
    """A pool file beside a copy of the llama-swap fixture."""
    shutil.copy(FIXTURES / "llama-swap.yaml", tmp_path / "llama-swap.yaml")
    data = {"llama_swap": "llama-swap.yaml", "accounts": list(accounts), "members": list(members)}
    data.update(top)
    path = tmp_path / "pool.yaml"
    path.write_text(yaml.safe_dump(copy.deepcopy(data)), encoding="utf-8")
    return path


def _refused(path):
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_pool_file(path)
    return excinfo.value


def _changed(entry, **changes):
    """*entry* with *changes* applied; a value of None drops the key."""
    merged = {**entry, **changes}
    return {key: value for key, value in merged.items() if value is not None}


# --- a valid roster ------------------------------------------------------


def test_the_fixture_roster_loads_one_local_and_one_hosted_member():
    cfg = config.load_pool_file(FIXTURE)

    assert [m.name for m in cfg.members] == ["coder-a", "strong-hosted"]
    local, hosted = cfg.members
    assert local.backing == "local"
    assert local.model == "model-a"
    assert local.account is None
    assert local.egress == "local"
    assert local.capacity == 1
    assert local.labels == ("code",)
    assert local.command == "pi --mode rpc --provider llama-swap --model model-a"
    assert hosted.backing == "hosted"
    assert hosted.account == "openrouter-main"
    assert hosted.egress == "no-train"
    assert hosted.capacity == 2
    assert hosted.labels == ("strong-model", "long-context")


def test_an_account_with_cap_4_loads():
    cfg = config.load_pool_file(FIXTURE)

    assert cfg.accounts == (config.Account("openrouter-main", 4, "OPENROUTER_API_KEY"),)


def test_a_relative_llama_swap_path_is_taken_from_the_pool_files_directory():
    cfg = config.load_pool_file(FIXTURE)

    assert cfg.llama_swap == FIXTURES / "llama-swap.yaml"
    assert "model-a" in cfg.swap.model_ids


def test_a_hosted_member_may_be_class_open(tmp_path):
    path = _write(tmp_path, members=[_changed(HOSTED, egress="open")])

    assert config.load_pool_file(path).members[0].egress == "open"


def test_a_roster_of_hosted_members_needs_no_llama_swap_configuration(tmp_path):
    path = _write(tmp_path, members=[HOSTED], llama_swap=None)
    (tmp_path / "llama-swap.yaml").unlink()

    cfg = config.load_pool_file(path)

    assert cfg.llama_swap is None
    assert cfg.swap is None


# --- accounts ------------------------------------------------------------


def test_an_account_with_cap_0_is_refused(tmp_path):
    path = _write(tmp_path, accounts=[_changed(ACCOUNT, cap=0)])

    error = _refused(path)

    assert error.entry == "account 'openrouter-main'"
    assert error.field == "cap"
    assert "openrouter-main" in str(error) and "cap" in str(error)


@pytest.mark.parametrize("cap", ["4", 2.5, True])
def test_an_account_cap_that_is_not_a_whole_number_is_refused(tmp_path, cap):
    error = _refused(_write(tmp_path, accounts=[_changed(ACCOUNT, cap=cap)]))

    assert (error.entry, error.field) == ("account 'openrouter-main'", "cap")


def test_a_duplicate_account_name_is_refused(tmp_path):
    second = _changed(ACCOUNT, cap=2, key_env="OTHER_KEY")

    error = _refused(_write(tmp_path, accounts=[ACCOUNT, second]))

    assert error.entry == "account 'openrouter-main'"
    assert error.field == "name"
    assert "more than once" in str(error)


def test_an_account_without_a_key_variable_is_refused(tmp_path):
    error = _refused(_write(tmp_path, accounts=[_changed(ACCOUNT, key_env=None)]))

    assert (error.entry, error.field) == ("account 'openrouter-main'", "key_env")


def test_an_entry_without_a_name_is_named_by_its_position(tmp_path):
    error = _refused(_write(tmp_path, accounts=[ACCOUNT, _changed(ACCOUNT, name=None)]))

    assert (error.entry, error.field) == ("accounts[2]", "name")


# --- no management key ---------------------------------------------------


def _schema_fields():
    """Every key the file may carry and every attribute the loaded form has."""
    names = set(config.SECTIONS) | set(config.ACCOUNT_FIELDS) | set(config.MEMBER_FIELDS)
    for cls in (config.PoolConfig, config.Account, config.Member):
        names |= {f.name for f in dataclasses.fields(cls)}
    return names


def test_the_schema_has_no_management_key_field():
    # A management key can mint spending keys, which is more authority than
    # the pool needs: an account holds the name of its one API key variable.
    assert config.ACCOUNT_FIELDS == ("name", "cap", "key_env")
    assert not [n for n in _schema_fields() if "manage" in n or "provision" in n]


def test_an_account_carrying_a_management_key_is_refused(tmp_path):
    account = _changed(ACCOUNT, management_key_env="OPENROUTER_MANAGEMENT_KEY")

    error = _refused(_write(tmp_path, accounts=[account]))

    assert (error.entry, error.field) == ("account 'openrouter-main'", "management_key_env")
    assert "unknown key" in str(error)


# --- local members -------------------------------------------------------


def test_a_local_member_whose_model_is_not_in_llama_swap_is_refused(tmp_path):
    error = _refused(_write(tmp_path, members=[_changed(LOCAL, model="model-z")]))

    assert error.entry == "member 'coder-a'"
    assert error.field == "model"
    assert "model-z" in str(error)


def test_a_local_member_naming_a_matrix_var_is_refused(tmp_path):
    # A var is a short name inside the matrix, not what a request loads.
    error = _refused(_write(tmp_path, members=[_changed(LOCAL, model="a")]))

    assert (error.entry, error.field) == ("member 'coder-a'", "model")
    assert "model-a" in str(error)


@pytest.mark.parametrize("key", ["profile", "selector"])
def test_a_local_member_naming_a_profile_or_selector_is_refused(tmp_path, key):
    member = _changed(LOCAL, model=None, **{key: "warm-coder"})

    error = _refused(_write(tmp_path, members=[member]))

    assert error.entry == "member 'coder-a'"
    assert error.field == key
    assert "concrete llama-swap model ID" in str(error)


def test_a_local_member_whose_model_is_a_selector_id_is_refused(tmp_path):
    # A selector resolves to a real model per request, so its ID is never
    # under llama-swap's models.
    error = _refused(_write(tmp_path, members=[_changed(LOCAL, model="warm-coder")]))

    assert (error.entry, error.field) == ("member 'coder-a'", "model")


def test_a_local_member_without_a_model_is_refused(tmp_path):
    error = _refused(_write(tmp_path, members=[_changed(LOCAL, model=None)]))

    assert (error.entry, error.field) == ("member 'coder-a'", "model")


@pytest.mark.parametrize("egress", ["open", "no-train"])
def test_a_local_member_with_a_hosted_class_is_refused(tmp_path, egress):
    error = _refused(_write(tmp_path, members=[_changed(LOCAL, egress=egress)]))

    assert (error.entry, error.field) == ("member 'coder-a'", "egress")


def test_a_local_member_naming_an_account_is_refused(tmp_path):
    member = _changed(LOCAL, account="openrouter-main")

    error = _refused(_write(tmp_path, members=[member]))

    assert (error.entry, error.field) == ("member 'coder-a'", "account")


def test_a_local_member_without_a_llama_swap_configuration_is_refused(tmp_path):
    error = _refused(_write(tmp_path, llama_swap=None))

    assert (error.entry, error.field) == (config.FILE, "llama_swap")


def test_a_llama_swap_configuration_that_cannot_be_read_is_refused_once(tmp_path):
    path = _write(tmp_path, llama_swap="missing.yaml")

    _, errors = config.check_pool_file(path)

    assert [(e.entry, e.field) for e in errors] == [(config.FILE, "llama_swap")]


def test_a_llama_swap_configuration_that_is_not_utf8_is_refused_once(tmp_path):
    path = _write(tmp_path)
    swap = tmp_path / "llama-swap.yaml"
    swap.write_bytes(b"# caf\xe9\n" + swap.read_bytes())

    _, errors = config.check_pool_file(path)

    assert [(e.path, e.entry, e.field) for e in errors] == [(path, config.FILE, "llama_swap")]
    assert str(swap) in str(errors[0]) and "not UTF-8" in str(errors[0])


# --- hosted members ------------------------------------------------------


def test_a_hosted_member_naming_an_undeclared_account_is_refused(tmp_path):
    error = _refused(_write(tmp_path, members=[_changed(HOSTED, account="other")]))

    assert error.entry == "member 'strong-hosted'"
    assert error.field == "account"
    assert "other" in str(error)


def test_a_hosted_member_without_an_account_is_refused(tmp_path):
    error = _refused(_write(tmp_path, members=[_changed(HOSTED, account=None)]))

    assert (error.entry, error.field) == ("member 'strong-hosted'", "account")


def test_a_hosted_member_with_class_local_is_refused(tmp_path):
    error = _refused(_write(tmp_path, members=[_changed(HOSTED, egress="local")]))

    assert (error.entry, error.field) == ("member 'strong-hosted'", "egress")


def test_a_member_on_an_account_refused_for_its_cap_is_not_called_undeclared(tmp_path):
    path = _write(tmp_path, accounts=[_changed(ACCOUNT, cap=0)])

    _, errors = config.check_pool_file(path)

    assert [(e.entry, e.field) for e in errors] == [("account 'openrouter-main'", "cap")]


# --- every member --------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("command", None),
        ("command", "  "),
        ("backing", None),
        ("backing", "cloud"),
        ("labels", "code"),
        ("labels", [1]),
        ("capacity", None),
        ("capacity", 0),
        ("capacity", "2"),
        ("egress", None),
        ("egress", "public"),
    ],
)
def test_a_member_field_that_is_missing_or_mistyped_is_refused(tmp_path, field, value):
    member = dict(LOCAL)
    member.pop(field)
    if value is not None:
        member[field] = value

    error = _refused(_write(tmp_path, members=[member]))

    assert (error.entry, error.field) == ("member 'coder-a'", field)


def test_a_member_without_labels_has_none(tmp_path):
    path = _write(tmp_path, members=[_changed(LOCAL, labels=None)])

    assert config.load_pool_file(path).members[0].labels == ()


def test_a_duplicate_member_name_is_refused(tmp_path):
    duplicate = _changed(LOCAL, model="model-b", command="pi --mode rpc --model model-b")

    error = _refused(_write(tmp_path, members=[LOCAL, duplicate]))

    assert (error.entry, error.field) == ("member 'coder-a'", "name")


def test_an_unknown_member_key_is_refused(tmp_path):
    error = _refused(_write(tmp_path, members=[_changed(LOCAL, colour="blue")]))

    assert (error.entry, error.field) == ("member 'coder-a'", "colour")


# A member's agent runs under the member's name, so a name the agent host
# would refuse is a member that can never start.
NOT_AGENT_NAMES = ["my member", "team/coder", "_coder"]


@pytest.mark.parametrize("name", NOT_AGENT_NAMES)
def test_a_member_name_that_cannot_name_an_agent_is_refused(tmp_path, name):
    _, errors = config.check_pool_file(_write(tmp_path, members=[_changed(LOCAL, name=name)]))

    assert [(e.entry, e.field) for e in errors] == [(f"member '{name}'", "name")]
    assert "cannot name an agent" in str(errors[0])


@pytest.mark.parametrize("name", NOT_AGENT_NAMES)
def test_a_console_name_that_cannot_name_an_agent_is_refused(tmp_path, name):
    slot = _changed(LOCAL, name=name, kind="console", command=None)

    _, errors = config.check_pool_file(_write(tmp_path, members=[slot]))

    assert [(e.entry, e.field) for e in errors] == [(f"member '{name}'", "name")]


def test_the_rule_for_a_member_name_is_the_agent_hosts_own():
    # the pool configuration imports nothing of the agent package, so it
    # repeats the rule; this is what keeps the two the same
    assert config.AGENT_NAME.pattern == statefiles._NAME_RE.pattern
    assert config.AGENT_NAME.flags == statefiles._NAME_RE.flags
    for name in NOT_AGENT_NAMES:
        with pytest.raises(ValueError):
            statefiles.AgentPaths.resolve(name)


@pytest.mark.parametrize("name", ["coder-a", "Coder_2.b", "9lives"])
def test_a_member_name_the_agent_host_takes_is_accepted(tmp_path, name):
    path = _write(tmp_path, members=[_changed(LOCAL, name=name)])

    assert [m.name for m in config.load_pool_file(path).members] == [name]


# --- the file ------------------------------------------------------------


def test_every_fault_in_the_file_is_reported_in_one_pass(tmp_path):
    path = _write(
        tmp_path,
        accounts=[_changed(ACCOUNT, cap=0)],
        members=[_changed(LOCAL, model="model-z"), _changed(HOSTED, egress="local")],
    )

    cfg, errors = config.check_pool_file(path)

    assert [(e.entry, e.field) for e in errors] == [
        ("account 'openrouter-main'", "cap"),
        ("member 'coder-a'", "model"),
        ("member 'strong-hosted'", "egress"),
    ]
    # Only the entries that stand as written are kept.
    assert cfg.accounts == () and cfg.members == ()


def test_a_missing_file_is_refused(tmp_path):
    error = _refused(tmp_path / "pool.yaml")

    assert (error.entry, error.field) == (config.FILE, "file")
    assert isinstance(error, SpecfloError)


def test_a_file_that_is_not_yaml_is_refused(tmp_path):
    path = tmp_path / "pool.yaml"
    path.write_text("accounts: [unclosed\n", encoding="utf-8")

    assert _refused(path).entry == config.FILE


def test_a_file_that_is_not_utf8_is_refused_naming_the_file(tmp_path):
    # Latin-1 from an editor's default: a fault of the file, not a traceback.
    path = tmp_path / "pool.yaml"
    path.write_bytes(b"accounts: []  # caf\xe9\n")

    error = _refused(path)

    assert (error.path, error.entry, error.field) == (path, config.FILE, "file")
    assert "not UTF-8" in str(error)


def test_a_file_that_is_not_a_mapping_is_refused(tmp_path):
    path = tmp_path / "pool.yaml"
    path.write_text("- accounts\n", encoding="utf-8")

    assert _refused(path).entry == config.FILE


def test_an_unknown_section_is_refused(tmp_path):
    error = _refused(_write(tmp_path, keys={"management": "sk-or-..."}))

    assert (error.entry, error.field) == (config.FILE, "keys")


def test_a_section_that_is_not_a_list_is_refused(tmp_path):
    path = tmp_path / "pool.yaml"
    path.write_text("accounts:\n  openrouter-main:\n    cap: 4\n", encoding="utf-8")

    error = _refused(path)

    assert (error.entry, error.field) == (config.FILE, "accounts")
