"""A local member's models file: the operator's provider, filtered to one model.

A local member runs against the rig's own llama-swap, and the provider that
reaches it is declared in the operator's models file with a base URL, an API
flavour, a key and the compatibility fields that make pi and that server
agree. The sandbox hides that file, so the member's generated one carries a
copy: the same provider, and the one model the member declares with the
fields the operator curated for it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from specflo.errors import SpecfloError
from specflo.pool import piconfig

from .test_piconfig import ACCOUNTS, LOCAL_MEMBER, NO_TRAIN_MEMBER

OTHER = {
    "id": "another-model",
    "name": "Another model",
    "contextWindow": 8192,
    "maxTokens": 1024,
}

DECLARED = {
    "id": "tc3",
    "name": "The model the member declares",
    "reasoning": True,
    "thinkingLevelMap": {"minimal": None, "low": "low", "medium": "medium", "high": "xhigh"},
    "contextWindow": 262144,
    "maxTokens": 40000,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    "compat": {"thinkingFormat": "chat-template"},
}

LOCAL_PROVIDER = {
    "baseUrl": "http://localhost:8080/v1",
    "api": "openai-completions",
    "apiKey": "a-key-the-rig-accepts",
    "authHeader": False,
    "compat": {"maxTokensField": "max_tokens", "thinkingFormat": "qwen-chat-template"},
    "models": [OTHER, DECLARED],
}

OPERATOR = {
    "providers": {
        "llama-cpp-local": LOCAL_PROVIDER,
        "somewhere-else": {"baseUrl": "https://example.invalid/v1", "models": []},
    }
}


@pytest.fixture
def models_file(tmp_path: Path) -> Path:
    path = tmp_path / "models.json"
    path.write_text(json.dumps(OPERATOR, indent=2), encoding="utf-8")
    return path


def generated(tmp_path: Path, models_file: Path, member=LOCAL_MEMBER) -> dict:
    directory = piconfig.create(
        tmp_path / "generated", member, ACCOUNTS, models_file=models_file
    )
    return json.loads(
        (directory / piconfig.MODELS_FILE).read_text(encoding="utf-8")
    )


def test_the_provider_is_the_operators_one_that_holds_the_model(
    tmp_path: Path, models_file: Path
) -> None:
    config = generated(tmp_path, models_file)

    assert list(config["providers"]) == ["llama-cpp-local"]


def test_the_provider_entry_equals_the_operators_but_for_its_models(
    tmp_path: Path, models_file: Path
) -> None:
    provider = generated(tmp_path, models_file)["providers"]["llama-cpp-local"]

    for field in ("baseUrl", "api", "apiKey", "authHeader", "compat"):
        assert provider[field] == LOCAL_PROVIDER[field]


def test_the_file_lists_the_declared_model_and_no_other(
    tmp_path: Path, models_file: Path
) -> None:
    provider = generated(tmp_path, models_file)["providers"]["llama-cpp-local"]

    assert [model["id"] for model in provider["models"]] == ["tc3"]


def test_the_curated_fields_of_the_model_are_kept(
    tmp_path: Path, models_file: Path
) -> None:
    (model,) = generated(tmp_path, models_file)["providers"]["llama-cpp-local"]["models"]

    assert model["contextWindow"] == DECLARED["contextWindow"]
    assert model["maxTokens"] == DECLARED["maxTokens"]
    assert model["thinkingLevelMap"] == DECLARED["thinkingLevelMap"]
    assert model["reasoning"] is True
    assert model == DECLARED


def test_the_generated_file_is_a_copy_the_operators_own_is_not_touched(
    tmp_path: Path, models_file: Path
) -> None:
    before = models_file.read_text(encoding="utf-8")

    config = generated(tmp_path, models_file)
    config["providers"]["llama-cpp-local"]["models"].clear()

    assert models_file.read_text(encoding="utf-8") == before
    assert generated(tmp_path, models_file)["providers"]["llama-cpp-local"]["models"]


def test_a_model_the_operators_file_does_not_hold_is_refused_by_name(
    tmp_path: Path, models_file: Path
) -> None:
    member = replace(LOCAL_MEMBER, model="a-model-nobody-has")

    with pytest.raises(SpecfloError) as refused:
        generated(tmp_path, models_file, member)

    assert "a-model-nobody-has" in str(refused.value)


def test_a_local_member_with_no_models_file_declared_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SpecfloError):
        piconfig.create(tmp_path / "generated", LOCAL_MEMBER, ACCOUNTS)


def test_a_hosted_member_is_untouched_by_the_operators_file(
    tmp_path: Path, models_file: Path
) -> None:
    directory = piconfig.create(
        tmp_path / "generated", NO_TRAIN_MEMBER, ACCOUNTS, models_file=models_file
    )

    config = json.loads((directory / piconfig.MODELS_FILE).read_text(encoding="utf-8"))
    assert list(config["providers"]) == [piconfig.PROVIDER]
