"""A member that declares a model the operator's models file does not hold.

A local member's generated models file is the operator's own provider copied
in and filtered to the one model the member declares. A member naming a model
that file does not hold would start against a provider whose model list is
empty, so the roster is refused where it is written rather than at the start
of a lease. The message names the model the member asked for and nothing
else: the operator's other models are not the member's business.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import yaml

from specflo.pool import config

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pool"

# model-d is in the llama-swap configuration and not in the operator's models
# file, so a member on it passes every other check and fails only this one.
HELD = "model-a"
UNHELD = "model-d"
OTHERS = ("model-b", "model-c")

LOCAL = {
    "name": "coder-a",
    "command": f"pi --mode rpc --provider llama-swap --model {HELD}",
    "backing": "local",
    "model": HELD,
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
ACCOUNT = {"name": "openrouter-main", "cap": 4, "key_env": "OPENROUTER_API_KEY"}


def on_model(model: str) -> dict:
    """The local member running *model*, its command selecting the same one."""
    return {
        **LOCAL, "model": model,
        "command": f"pi --mode rpc --provider llama-swap --model {model}",
    }


def write_pool(tmp_path: Path, members, *, models_file: str | None = "models.json") -> Path:
    """A pool directory holding *members*, the rig's llama-swap configuration
    and the operator's models file."""
    directory = tmp_path / "pool"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "llama-swap.yaml", directory / "llama-swap.yaml")
    shutil.copy(FIXTURES / "models.json", directory / "models.json")
    data = {
        "llama_swap": "llama-swap.yaml",
        "accounts": [ACCOUNT],
        "members": copy.deepcopy(list(members)),
    }
    if models_file is not None:
        data["models_file"] = models_file
    path = directory / config.POOL_FILE
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def faults_of(path: Path) -> list[config.ConfigError]:
    _, errors = config.check_pool_file(path)
    return errors


def test_a_local_member_on_a_model_the_file_does_not_hold_is_refused(tmp_path: Path) -> None:
    faults = faults_of(write_pool(tmp_path, [on_model(UNHELD)]))

    assert [(f.entry, f.field) for f in faults] == [("member 'coder-a'", "model")]
    assert UNHELD in str(faults[0])


def test_the_message_names_no_other_model_the_file_holds(tmp_path: Path) -> None:
    faults = faults_of(write_pool(tmp_path, [on_model(UNHELD)]))

    assert not any(other in str(faults[0]) for other in OTHERS)


def test_a_local_member_on_a_model_the_file_holds_is_no_fault(tmp_path: Path) -> None:
    assert faults_of(write_pool(tmp_path, [LOCAL])) == []


def test_a_hosted_member_is_not_checked_against_the_operators_file(tmp_path: Path) -> None:
    # A hosted member's models file is written for its provider, not copied
    # from the operator's, so its model is no business of that file.
    assert faults_of(write_pool(tmp_path, [LOCAL, HOSTED])) == []


def test_a_pool_file_that_names_no_models_file_is_not_checked(tmp_path: Path) -> None:
    # There is nothing to check against; the copy at the start of a lease is
    # what refuses such a member, and it names the missing declaration.
    path = write_pool(tmp_path, [on_model(UNHELD)], models_file=None)

    assert faults_of(path) == []


def test_a_models_file_that_cannot_be_parsed_is_reported_once_and_not_as_the_model(
    tmp_path: Path,
) -> None:
    # The path is what this file's declaration is checked for; content that is
    # not a provider map leaves no model ids to check a member against, and
    # the copy at the start of a lease is what reports it.
    path = write_pool(tmp_path, [on_model(UNHELD)])
    (path.parent / "models.json").write_text("not json at all", encoding="utf-8")

    assert faults_of(path) == []
