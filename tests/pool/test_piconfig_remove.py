"""The end of a lease takes the generated directory with it, local ones too.

A directory is generated for every member now, so every lease has one to
remove and a local member's is no longer the case that removes nothing. What
guards the removal is where the path is, never what is in it: the member may
write in its directory, and may take anything out of it. Only a direct child
of the pool's configuration root is removed, whatever the daemon's record of
the member names; a path that reaches the remover by mistake may be the
operator's own configuration, and it is refused rather than removed.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from specflo.agent.statefiles import AgentPaths
from specflo.errors import SpecfloError
from specflo.pool import piconfig, runner

from .test_piconfig import ACCOUNTS, LOCAL_MEMBER, NO_TRAIN_MEMBER, operator_models


def test_a_local_members_directory_is_gone_when_its_lease_ends(tmp_path: Path) -> None:
    directory = piconfig.create(tmp_path / 'generated', LOCAL_MEMBER, ACCOUNTS, operator_models(tmp_path))
    # what pi wrote there during the lease goes with it
    (directory / "sessions").mkdir()
    (directory / "sessions" / "one.jsonl").write_text("{}\n", encoding="utf-8")

    piconfig.remove(directory, tmp_path / 'generated')

    assert not directory.exists()
    assert list((tmp_path / "generated").iterdir()) == []


def test_one_members_lease_ending_leaves_another_members_directory(tmp_path: Path) -> None:
    mine = piconfig.create(tmp_path / 'generated', LOCAL_MEMBER, ACCOUNTS, operator_models(tmp_path))
    theirs = piconfig.create(tmp_path / 'generated', NO_TRAIN_MEMBER, ACCOUNTS)

    piconfig.remove(mine, tmp_path / 'generated')

    assert not mine.exists()
    assert theirs.is_dir()


def test_no_marker_is_written_into_a_generated_directory(tmp_path: Path) -> None:
    directory = piconfig.create(tmp_path / 'generated', NO_TRAIN_MEMBER, ACCOUNTS)

    assert ".specflo-pool-member" not in [p.name for p in directory.iterdir()]


def test_a_directory_its_member_emptied_is_removed(tmp_path: Path) -> None:
    directory = piconfig.create(tmp_path / 'generated', LOCAL_MEMBER, ACCOUNTS, operator_models(tmp_path))
    for entry in directory.iterdir():
        if entry.is_dir():
            import shutil
            shutil.rmtree(entry)
        else:
            entry.unlink()

    piconfig.remove(directory, tmp_path / 'generated')

    assert not directory.exists()


def _outside(tmp_path: Path) -> Path:
    return tmp_path / "agent"


def _the_root(tmp_path: Path) -> Path:
    return tmp_path / "generated"


def _two_below(tmp_path: Path) -> Path:
    return tmp_path / "generated" / "hosted-1-abc" / "sessions"


@pytest.mark.parametrize("where", [_outside, _the_root, _two_below])
def test_a_path_that_is_no_direct_child_of_the_root_is_refused_and_left(
    tmp_path: Path, where
) -> None:
    piconfig.create(tmp_path / 'generated', NO_TRAIN_MEMBER, ACCOUNTS)
    theirs = where(tmp_path)
    theirs.mkdir(parents=True, exist_ok=True)
    (theirs / "auth.json").write_text('{"key": "secret"}', encoding="utf-8")
    before = sorted(str(p) for p in theirs.rglob("*"))

    with pytest.raises(SpecfloError) as refused:
        piconfig.remove(theirs, tmp_path / 'generated')

    assert str(theirs) in str(refused.value)
    assert sorted(str(p) for p in theirs.rglob("*")) == before


def test_a_path_that_climbs_out_of_the_root_is_refused(tmp_path: Path) -> None:
    theirs = tmp_path / "agent"
    theirs.mkdir()
    (tmp_path / "generated").mkdir()

    with pytest.raises(SpecfloError):
        piconfig.remove(tmp_path / "generated" / ".." / "agent", tmp_path / "generated")

    assert theirs.is_dir()


@pytest.fixture
def state(tmp_path, monkeypatch) -> AgentPaths:
    from specflo.agent.statefiles import ENV_STATE_DIR

    monkeypatch.setenv(ENV_STATE_DIR, str(tmp_path / "state"))
    return AgentPaths.resolve("hosted-1").ensure()


def test_the_stop_removes_an_emptied_directory_by_the_daemons_record(
    tmp_path: Path, state: AgentPaths
) -> None:
    root = tmp_path / "generated"
    directory = piconfig.create(root, NO_TRAIN_MEMBER, ACCOUNTS)
    for entry in list(directory.iterdir()):
        entry.unlink() if entry.is_file() else entry.rmdir()
    runner._record_config(state, directory, root)

    runner._forget(state)

    assert not directory.exists()
    assert not (state.root / runner.CONFIG_DIR_FILE).exists()
    assert not (state.root / runner.CONFIG_ROOT_FILE).exists()


@pytest.mark.parametrize("where", [_outside, _the_root, _two_below])
def test_a_record_naming_a_path_no_direct_child_of_the_root_leaves_it_and_logs_it(
    tmp_path: Path, state: AgentPaths, caplog, where
) -> None:
    root = tmp_path / "generated"
    piconfig.create(root, NO_TRAIN_MEMBER, ACCOUNTS)
    theirs = where(tmp_path)
    theirs.mkdir(parents=True, exist_ok=True)
    (theirs / "auth.json").write_text('{"key": "secret"}', encoding="utf-8")
    before = sorted(str(p) for p in theirs.rglob("*"))
    runner._record_config(state, theirs, root)

    with caplog.at_level(logging.WARNING, logger=runner.__name__):
        runner._forget(state)

    assert sorted(str(p) for p in theirs.rglob("*")) == before
    assert str(theirs) in caplog.text


def test_ending_a_lease_twice_removes_nothing_the_second_time(tmp_path: Path) -> None:
    directory = piconfig.create(tmp_path / 'generated', LOCAL_MEMBER, ACCOUNTS, operator_models(tmp_path))

    piconfig.remove(directory, tmp_path / 'generated')
    piconfig.remove(directory, tmp_path / 'generated')

    assert not directory.exists()


def test_nothing_to_remove_is_not_a_failure(tmp_path: Path) -> None:
    piconfig.remove(None, tmp_path)
    piconfig.remove(tmp_path / "never-was", tmp_path)


@pytest.fixture
def take_rights():
    """Take rights from a directory for one test. Its teardown gives each one
    that is still there owner rwx back, passed or failed, so pytest can remove
    its temporary directory."""
    taken: list[Path] = []

    def take(directory: Path, mode: int) -> None:
        taken.append(directory)
        directory.chmod(mode)

    yield take
    # a parent first, so its children can be seen again
    for directory in sorted(taken, key=lambda path: len(path.parts)):
        if directory.is_dir():
            directory.chmod(0o700)


def test_a_directory_the_member_made_unreadable_is_removed_all_the_same(
    tmp_path: Path, take_rights
) -> None:
    directory = piconfig.create(tmp_path / 'generated', NO_TRAIN_MEMBER, ACCOUNTS)
    locked = directory / "sessions" / "locked"
    locked.mkdir(parents=True)
    (locked / "one.jsonl").write_text("{}\n", encoding="utf-8")
    take_rights(locked, 0o000)
    take_rights(directory / "sessions", 0o500)

    piconfig.remove(directory, tmp_path / 'generated')

    assert not directory.exists()
