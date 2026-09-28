"""Every member's git has the operator's identity, and nothing else of theirs.

A member runs with a home the sandbox sweeps, so its git has no global
configuration and refuses to commit. The pool writes one into the member's
generated directory: the user.name and user.email the daemon's git reports
outside any repository, and no other key, so no credential helper, include
or signing key of the operator's reaches the member. With no identity to give,
no file is written.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from specflo.pool import launch, piconfig

from .test_piconfig import ACCOUNTS, NO_TRAIN_MEMBER


def git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return done.stdout


@pytest.fixture
def operator_git(tmp_path, monkeypatch):
    """Set what the daemon's git reports: the given keys in its global file,
    beside keys a member must never get."""

    def configure(**keys: str) -> Path:
        path = tmp_path / "operator.gitconfig"
        path.write_text("", encoding="utf-8")
        for key, value in {
            "credential.helper": "store",
            "include.path": str(tmp_path / "more.gitconfig"),
            "user.signingkey": "ABCDEF",
            "commit.gpgsign": "true",
            **{f"user.{name}": value for name, value in keys.items()},
        }.items():
            git("config", "--file", str(path), key, value)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(path))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        return path

    return configure


def generated(tmp_path: Path) -> Path:
    return piconfig.create(tmp_path / "generated", NO_TRAIN_MEMBER, ACCOUNTS)


def keys_in(path: Path) -> list[str]:
    return git("config", "--file", str(path), "--list").splitlines()


def test_the_file_holds_the_operators_name_and_email_and_no_other_key(tmp_path, operator_git):
    operator_git(name="Test Operator", email="operator@example.com")

    path = generated(tmp_path) / launch.GITCONFIG_FILE

    assert keys_in(path) == ["user.name=Test Operator", "user.email=operator@example.com"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_identity_is_the_one_reported_outside_any_repository(
    tmp_path, operator_git, monkeypatch
):
    operator_git(name="Test Operator", email="operator@example.com")
    repository = tmp_path / "repository"
    repository.mkdir()
    git("init", "-q", cwd=repository)
    git("config", "user.name", "Repo Local", cwd=repository)
    monkeypatch.chdir(repository)

    path = generated(tmp_path) / launch.GITCONFIG_FILE

    assert keys_in(path) == ["user.name=Test Operator", "user.email=operator@example.com"]


def test_with_one_key_reported_the_file_holds_that_one(tmp_path, operator_git):
    operator_git(email="operator@example.com")

    path = generated(tmp_path) / launch.GITCONFIG_FILE

    assert keys_in(path) == ["user.email=operator@example.com"]


def test_with_no_identity_no_file_is_written(tmp_path, operator_git):
    operator_git()

    assert not (generated(tmp_path) / launch.GITCONFIG_FILE).exists()


def test_a_value_git_would_read_as_syntax_comes_back_as_it_was(tmp_path, operator_git):
    name = 'Tester "the Op" O\'Hara \\ #1; ok'
    operator_git(name=name)

    path = generated(tmp_path) / launch.GITCONFIG_FILE

    assert git("config", "--file", str(path), "user.name") == name + "\n"
