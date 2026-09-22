"""The end of a lease takes the generated directory with it, local ones too.

A directory is generated for every member now, so every lease has one to
remove and a local member's is no longer the case that removes nothing. What
guards the removal is the marker written into a generated directory: a path
that reaches the remover by mistake may be the operator's own configuration,
and one without the marker is refused rather than removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from specflo.errors import SpecfloError
from specflo.pool import piconfig

from .test_piconfig import ACCOUNTS, LOCAL_MEMBER, NO_TRAIN_MEMBER


def test_a_local_members_directory_is_gone_when_its_lease_ends(tmp_path: Path) -> None:
    directory = piconfig.create(tmp_path, LOCAL_MEMBER, ACCOUNTS)
    # what pi wrote there during the lease goes with it
    (directory / "sessions").mkdir()
    (directory / "sessions" / "one.jsonl").write_text("{}\n", encoding="utf-8")

    piconfig.remove(directory)

    assert not directory.exists()
    assert list(tmp_path.iterdir()) == []


def test_one_members_lease_ending_leaves_another_members_directory(tmp_path: Path) -> None:
    mine = piconfig.create(tmp_path, LOCAL_MEMBER, ACCOUNTS)
    theirs = piconfig.create(tmp_path, NO_TRAIN_MEMBER, ACCOUNTS)

    piconfig.remove(mine)

    assert not mine.exists()
    assert theirs.is_dir()


def test_a_directory_that_carries_no_marker_is_refused(tmp_path: Path) -> None:
    # It could be the operator's own pi configuration, reached by mistake.
    theirs = tmp_path / "agent"
    (theirs / "sessions").mkdir(parents=True)
    (theirs / "auth.json").write_text('{"key": "secret"}', encoding="utf-8")

    with pytest.raises(SpecfloError) as refused:
        piconfig.remove(theirs)

    assert str(theirs) in str(refused.value)
    assert (theirs / "auth.json").is_file()


def test_a_marker_a_member_removed_during_its_lease_refuses_the_removal(
    tmp_path: Path,
) -> None:
    directory = piconfig.create(tmp_path, LOCAL_MEMBER, ACCOUNTS)
    (directory / piconfig.MARKER).unlink()

    with pytest.raises(SpecfloError):
        piconfig.remove(directory)

    assert directory.is_dir()


def test_ending_a_lease_twice_removes_nothing_the_second_time(tmp_path: Path) -> None:
    directory = piconfig.create(tmp_path, LOCAL_MEMBER, ACCOUNTS)

    piconfig.remove(directory)
    piconfig.remove(directory)

    assert not directory.exists()


def test_nothing_to_remove_is_not_a_failure(tmp_path: Path) -> None:
    piconfig.remove(None)
    piconfig.remove(tmp_path / "never-was")
