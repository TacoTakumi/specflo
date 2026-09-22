"""The model a member declares and the model its harness command selects.

A member runs the one model it names: the generated models file holds that
one alone, and the co-residency ledger accounts for that one. A command that
selects another would run the member on a model neither of those knows
about, so the roster is refused where the two disagree, and refused again
where neither the declaration nor the command names a model at all.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import yaml

from specflo.pool import config

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pool"

LOCAL = {
    "name": "coder-a",
    "command": "pi --mode rpc --provider llama-swap --model model-a",
    "backing": "local",
    "model": "model-a",
    "labels": ["code"],
    "capacity": 1,
    "egress": "local",
}
HOSTED = {
    "name": "strong-hosted",
    "command": "pi --mode rpc --provider openrouter --model vendor/strong",
    "backing": "hosted",
    "account": "openrouter-main",
    "labels": ["strong-model"],
    "capacity": 2,
    "egress": "no-train",
}
CONSOLE = {
    "name": "console-a",
    "kind": "console",
    "backing": "local",
    "model": "model-a",
    "labels": [],
    "capacity": 1,
    "egress": "local",
}
ACCOUNT = {"name": "openrouter-main", "cap": 4, "key_env": "OPENROUTER_API_KEY"}


def write_pool(tmp_path: Path, members) -> Path:
    directory = tmp_path / "pool"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "llama-swap.yaml", directory / "llama-swap.yaml")
    shutil.copy(FIXTURES / "models.json", directory / "models.json")
    data = {
        "llama_swap": "llama-swap.yaml",
        "models_file": "models.json",
        "accounts": [ACCOUNT],
        "members": copy.deepcopy(list(members)),
    }
    path = directory / config.POOL_FILE
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def faults_of(path: Path) -> list[config.ConfigError]:
    _, errors = config.check_pool_file(path)
    return errors


def test_a_command_that_selects_the_declared_model_is_no_fault(tmp_path: Path) -> None:
    assert faults_of(write_pool(tmp_path, [LOCAL, HOSTED])) == []


def test_a_command_that_selects_another_model_is_refused(tmp_path: Path) -> None:
    member = {**LOCAL, "command": "pi --mode rpc --provider llama-swap --model model-b"}

    faults = faults_of(write_pool(tmp_path, [member]))

    assert [(f.entry, f.field) for f in faults] == [("member 'coder-a'", "command")]
    assert "model-b" in str(faults[0]) and "model-a" in str(faults[0])


def test_the_flag_written_with_an_equals_sign_is_read_the_same_way(tmp_path: Path) -> None:
    member = {**LOCAL, "command": "pi --mode rpc --provider llama-swap --model=model-b"}

    faults = faults_of(write_pool(tmp_path, [member]))

    assert [(f.entry, f.field) for f in faults] == [("member 'coder-a'", "command")]


def test_a_second_model_flag_that_disagrees_is_refused(tmp_path: Path) -> None:
    member = {**LOCAL, "command": "pi --mode rpc --model model-a --model model-b"}

    faults = faults_of(write_pool(tmp_path, [member]))

    assert [(f.entry, f.field) for f in faults] == [("member 'coder-a'", "command")]


def test_a_member_that_names_no_model_at_all_is_refused(tmp_path: Path) -> None:
    member = {k: v for k, v in HOSTED.items() if k != "model"}
    member["command"] = "pi --mode rpc --provider openrouter"

    faults = faults_of(write_pool(tmp_path, [member]))

    assert [(f.entry, f.field) for f in faults] == [("member 'strong-hosted'", "model")]


def test_a_command_that_names_the_model_alone_is_no_fault(tmp_path: Path) -> None:
    # A hosted member need not repeat in a declaration what its command says.
    assert faults_of(write_pool(tmp_path, [{k: v for k, v in HOSTED.items()}])) == []


def test_a_provider_prefix_on_one_side_is_the_same_model(tmp_path: Path) -> None:
    # pi names a provider's model with the provider in front; it is the same
    # model as the one the declaration names without it.
    member = {
        **HOSTED,
        "model": "vendor/strong",
        "command": "pi --mode rpc --provider openrouter --model openrouter/vendor/strong",
    }

    assert faults_of(write_pool(tmp_path, [member])) == []


def test_a_declaration_the_command_leaves_out_is_no_fault(tmp_path: Path) -> None:
    # The generated models file holds the declared model alone, so a command
    # that selects none has one model to resolve to.
    member = {**LOCAL, "command": "pi --mode rpc --provider llama-swap"}

    assert faults_of(write_pool(tmp_path, [member])) == []


def test_a_console_is_not_held_to_this(tmp_path: Path) -> None:
    # The pool never starts a console, so it has no command to read.
    assert faults_of(write_pool(tmp_path, [CONSOLE])) == []


def test_a_declaration_already_at_fault_is_not_reported_twice(tmp_path: Path) -> None:
    member = {k: v for k, v in LOCAL.items() if k != "model"}
    member["command"] = "pi --mode rpc --provider llama-swap"

    faults = faults_of(write_pool(tmp_path, [member]))

    assert [(f.entry, f.field) for f in faults] == [("member 'coder-a'", "model")]
    assert "required" in str(faults[0])
