"""Where the operator's models file lives, and what the pool checks about it.

A local member's generated models file is a copy of the operator's, filtered
to the one model the member declares, so the pool has to be told where the
operator's file is. The declaration is a path in the pool file, and what is
checked here is the path: a file that is not there, one that cannot be read,
one that is not a regular file, and one that would block the daemon if it
were opened.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from specflo.pool import config as pool_config

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "pool"

MODELS = {"providers": {"llama-swap": {"baseUrl": "http://127.0.0.1:8080/v1"}}}

POOL = """\
models_file: {models_file}
members:
  - name: hosted-1
    command: pi --mode rpc --model openrouter/some-vendor/some-model
    backing: hosted
    model: some-vendor/some-model
    account: team-a
    labels: []
    capacity: 1
    egress: no-train
accounts:
  - name: team-a
    cap: 2
    key_env: TEAM_A_KEY
pools:
  - name: rebasers
    definition: rebaser
    members: [hosted-1]
    size: 1
    idle_default: 10m
    idle_max: 4h
"""


def write_pool(tmp_path: Path, models_file: str) -> Path:
    directory = tmp_path / "pool"
    (directory / "definitions").mkdir(parents=True, exist_ok=True)
    (directory / "definitions" / "rebaser.md").write_text(
        "---\nrole: Rebases the work branch\n---\n\nYou are the rebaser.\n",
        encoding="utf-8",
    )
    (directory / "pool.yaml").write_text(
        POOL.format(models_file=models_file), encoding="utf-8"
    )
    return directory


def faults_of(directory: Path) -> list[str]:
    _, errors = pool_config.load_pool_config(directory)
    return [str(error) for error in errors]


def test_the_declaration_is_read_and_the_path_kept(tmp_path: Path) -> None:
    models = tmp_path / "models.json"
    models.write_text(json.dumps(MODELS), encoding="utf-8")

    config, errors = pool_config.load_pool_config(write_pool(tmp_path, str(models)))

    assert errors == []
    assert config.models_file == models


def test_a_relative_path_is_taken_from_the_pool_file(tmp_path: Path) -> None:
    directory = write_pool(tmp_path, "models.json")
    (directory / "models.json").write_text(json.dumps(MODELS), encoding="utf-8")

    config, errors = pool_config.load_pool_config(directory)

    assert errors == []
    assert config.models_file == directory / "models.json"


def test_a_file_that_is_not_there_fails_and_the_message_names_the_key(
    tmp_path: Path,
) -> None:
    faults = faults_of(write_pool(tmp_path, str(tmp_path / "never-was.json")))

    assert any("models_file" in fault and "never-was.json" in fault for fault in faults)


def test_a_path_that_is_a_directory_fails_and_the_message_names_the_key(
    tmp_path: Path,
) -> None:
    (tmp_path / "a-directory").mkdir()

    faults = faults_of(write_pool(tmp_path, str(tmp_path / "a-directory")))

    assert any("models_file" in fault for fault in faults)


def test_a_file_that_cannot_be_read_fails_and_the_message_names_the_key(
    tmp_path: Path,
) -> None:
    if os.getuid() == 0:
        pytest.skip("the superuser reads a file whatever its mode says")
    models = tmp_path / "models.json"
    models.write_text(json.dumps(MODELS), encoding="utf-8")
    models.chmod(0o000)

    faults = faults_of(write_pool(tmp_path, str(models)))

    assert any("models_file" in fault for fault in faults)


def test_a_named_pipe_is_refused_rather_than_read(tmp_path: Path) -> None:
    # Opening one waits for a writer that never comes, and the daemon would
    # wait with it, so the check must be of what the path is, not of what it
    # holds. This test does not return at all if that goes wrong.
    pipe = tmp_path / "models.json"
    os.mkfifo(pipe)

    faults = faults_of(write_pool(tmp_path, str(pipe)))

    assert any("models_file" in fault for fault in faults)


def test_a_pool_file_of_hosted_members_that_names_none_is_no_fault(tmp_path: Path) -> None:
    directory = write_pool(tmp_path, "models.json")
    (directory / "pool.yaml").write_text(
        "\n".join(
            line
            for line in POOL.format(models_file="models.json").splitlines()
            if not line.startswith("models_file:")
        )
        + "\n",
        encoding="utf-8",
    )

    config, errors = pool_config.load_pool_config(directory)

    assert errors == []
    assert config.models_file is None


def test_a_declaration_that_is_not_a_path_fails(tmp_path: Path) -> None:
    faults = faults_of(write_pool(tmp_path, "[]"))

    assert any("models_file" in fault for fault in faults)


LOCAL_POOL = """\
llama_swap: llama-swap.yaml
members:
  - name: local-1
    command: pi --mode rpc
    backing: local
    model: model-a
    labels: []
    capacity: 1
    egress: local
pools:
  - name: rebasers
    definition: rebaser
    members: [local-1]
    size: 1
    idle_default: 10m
    idle_max: 4h
"""


def test_a_pool_file_with_a_local_member_that_names_no_models_file_fails(
    tmp_path: Path,
) -> None:
    # Every lease of such a member would fail at its start, where its models
    # file is copied from the operator's.
    directory = write_pool(tmp_path, "models.json")
    shutil.copy(FIXTURES / "llama-swap.yaml", directory / "llama-swap.yaml")
    (directory / "pool.yaml").write_text(LOCAL_POOL, encoding="utf-8")

    faults = faults_of(directory)

    assert len(faults) == 1
    assert "models_file" in faults[0]
    assert "local" in faults[0]
