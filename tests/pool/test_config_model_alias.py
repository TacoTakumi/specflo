"""A local member's model named by its llama-swap config ID or by an alias.

llama-swap answers a model under its config ID and under every alias the
configuration gives it, and an operator's pi models file may name the model
by either. The matrix sets and the ledger speak config IDs; the member's
generated models file is a copy of the entry the operator's file holds. So a
member may declare either name: the pool accounts for the model under its
config ID, and copies the operator's entry under whichever name it is held.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from specflo.pool import config, piconfig

# The rig's shape: pi names the first model by a mixed-case alias, and the
# second by its config ID.
SWAP = {
    "models": {
        "coder-q3": {
            "cmd": "llama-server --port ${PORT} -m /models/coder.gguf",
            "aliases": ["Coder-Q3", "coder-small"],
        },
        "helper-q4": {
            "cmd": "llama-server --port ${PORT} -m /models/helper.gguf",
            "aliases": ["Helper-Q4"],
        },
        "other-q8": {"cmd": "llama-server --port ${PORT} -m /models/other.gguf"},
    },
    "matrix": {
        "vars": {"c": "coder-q3", "h": "helper-q4", "o": "other-q8"},
        "sets": {"pair": "(c | o) & h"},
    },
}
OPERATOR = {
    "providers": {
        "llama-cpp-local": {
            "baseUrl": "http://127.0.0.1:8080/v1",
            "api": "openai-completions",
            "models": [
                {"id": "Coder-Q3", "name": "Coder", "contextWindow": 65536},
                {"id": "helper-q4", "name": "Helper", "contextWindow": 32768},
            ],
        }
    }
}


def member(name: str, model: str, selected: str | None = None) -> dict:
    return {
        "name": name,
        "command": f"pi --mode rpc --provider llama-cpp-local --model {selected or model}",
        "backing": "local",
        "model": model,
        "labels": ["code"],
        "capacity": 1,
        "egress": "local",
    }


def write_pool(tmp_path: Path, members: list[dict], operator: dict = OPERATOR) -> Path:
    directory = tmp_path / "pool"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "llama-swap.yaml").write_text(yaml.safe_dump(SWAP), encoding="utf-8")
    (directory / "models.json").write_text(json.dumps(operator), encoding="utf-8")
    path = directory / config.POOL_FILE
    path.write_text(yaml.safe_dump({
        "llama_swap": "llama-swap.yaml",
        "models_file": "models.json",
        "members": members,
    }), encoding="utf-8")
    return path


def checked(path: Path):
    return config.check_pool_file(path)


def only(pool: config.PoolConfig) -> config.Member:
    (one,) = pool.members
    return one


# --- either name validates, and the ledger's name is the config ID ----------


def test_a_member_declaring_an_alias_validates_under_the_config_id(tmp_path: Path) -> None:
    pool, errors = checked(write_pool(tmp_path, [member("coder", "Coder-Q3")]))

    assert errors == []
    assert only(pool).model == "coder-q3"
    assert pool.swap.fits([only(pool).model, "helper-q4"])


def test_a_member_declaring_the_config_id_of_a_model_pi_holds_by_alias_validates(
    tmp_path: Path,
) -> None:
    pool, errors = checked(
        write_pool(tmp_path, [member("coder", "coder-q3", selected="Coder-Q3")])
    )

    assert errors == []
    assert only(pool).model == "coder-q3"


def test_a_model_pi_holds_by_its_config_id_is_unchanged(tmp_path: Path) -> None:
    pool, errors = checked(write_pool(tmp_path, [member("helper", "helper-q4")]))

    assert errors == []
    assert only(pool).model == "helper-q4"


# --- the generated models file carries the operator's entry -----------------


def test_the_generated_file_holds_the_operators_entry_for_an_alias(tmp_path: Path) -> None:
    path = write_pool(tmp_path, [member("coder", "coder-q3", selected="Coder-Q3")])
    pool, _ = checked(path)

    generated = piconfig.local_models_config(path.parent / "models.json", only(pool))

    (provider,) = generated["providers"].values()
    assert provider["models"] == [OPERATOR["providers"]["llama-cpp-local"]["models"][0]]


def test_the_generated_file_is_the_same_whichever_name_is_declared(tmp_path: Path) -> None:
    by_alias, _ = checked(write_pool(tmp_path / "a", [member("coder", "Coder-Q3")]))
    by_id, _ = checked(
        write_pool(tmp_path / "b", [member("coder", "coder-q3", selected="Coder-Q3")])
    )
    models = tmp_path / "a" / "pool" / "models.json"

    assert piconfig.local_models_config(models, only(by_alias)) == (
        piconfig.local_models_config(models, only(by_id))
    )


# --- what is still refused ---------------------------------------------------


def test_a_name_that_is_neither_a_config_id_nor_an_alias_is_refused(tmp_path: Path) -> None:
    _, errors = checked(write_pool(tmp_path, [member("coder", "Coder-Q9")]))

    assert [(e.entry, e.field) for e in errors] == [("member 'coder'", "model")]
    assert "Coder-Q9" in str(errors[0])


def test_a_model_the_operator_holds_under_no_name_is_refused(tmp_path: Path) -> None:
    _, errors = checked(write_pool(tmp_path, [member("other", "other-q8")]))

    assert [(e.entry, e.field) for e in errors] == [("member 'other'", "model")]
    assert "other-q8" in str(errors[0])


def test_a_command_selecting_another_model_is_refused(tmp_path: Path) -> None:
    _, errors = checked(
        write_pool(tmp_path, [member("coder", "Coder-Q3", selected="Helper-Q4")])
    )

    assert [(e.entry, e.field) for e in errors] == [("member 'coder'", "command")]


def test_an_operator_file_holding_the_model_under_two_names_is_refused(tmp_path: Path) -> None:
    # Two entries for one model may differ in every setting, and nothing says
    # which the member should run with.
    operator = json.loads(json.dumps(OPERATOR))
    operator["providers"]["llama-cpp-local"]["models"].append(
        {"id": "coder-small", "name": "Coder small", "contextWindow": 8192}
    )
    _, errors = checked(write_pool(tmp_path, [member("coder", "coder-q3")], operator))

    assert [(e.entry, e.field) for e in errors] == [("member 'coder'", "model")]
    assert "Coder-Q3" in str(errors[0]) and "coder-small" in str(errors[0])

