"""A member whose definition names a skill the operator does not have.

A definition's skills are the member's whole set: the sandbox hides every
other place pi would find one, so a skill that is not copied in is a skill
the member runs without. A member that would start short of what its role
names is refused instead, and the message names the skill so the admin knows
which line of the definition to fix. Nothing of the lease is left behind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from specflo.errors import SpecfloError
from specflo.pool import piconfig
from specflo.pool.launch import LaunchError

from .test_piconfig import ACCOUNTS, NO_TRAIN_MEMBER

SKILL = "---\nname: rebaser\n---\n\nThe rebaser skill.\n"


@pytest.fixture
def skills(tmp_path: Path) -> Path:
    directory = tmp_path / "agent" / "skills"
    (directory / "rebaser").mkdir(parents=True)
    (directory / "rebaser" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    return directory


def create(tmp_path: Path, skills: Path | None, *names: str) -> Path:
    return piconfig.create(
        tmp_path / "generated", NO_TRAIN_MEMBER, ACCOUNTS,
        skills=names, skills_from=skills,
    )


def test_a_skill_that_is_not_there_is_refused_and_named(tmp_path: Path, skills: Path) -> None:
    with pytest.raises(LaunchError) as excinfo:
        create(tmp_path, skills, "rebaser", "never-was")

    assert isinstance(excinfo.value, SpecfloError)
    assert "never-was" in str(excinfo.value)
    assert NO_TRAIN_MEMBER.name in str(excinfo.value)


def test_nothing_of_the_lease_is_left_behind(tmp_path: Path, skills: Path) -> None:
    with pytest.raises(LaunchError):
        create(tmp_path, skills, "rebaser", "never-was")

    assert not (tmp_path / "generated").exists()


def test_a_link_that_leads_nowhere_is_refused(tmp_path: Path, skills: Path) -> None:
    (skills / "gone").symlink_to(tmp_path / "never-was")

    with pytest.raises(LaunchError, match="gone"):
        create(tmp_path, skills, "gone")


def test_a_skill_that_is_a_file_rather_than_a_directory_is_refused(
    tmp_path: Path, skills: Path
) -> None:
    (skills / "loose.md").write_text(SKILL, encoding="utf-8")

    with pytest.raises(LaunchError, match="loose.md"):
        create(tmp_path, skills, "loose.md")


def test_a_name_that_is_a_path_rather_than_a_skill_is_refused(
    tmp_path: Path, skills: Path
) -> None:
    # A name is a directory in the operator's skills directory and nothing
    # else; one that walks out of it would copy whatever it reached.
    (tmp_path / "agent" / "secrets").mkdir()

    with pytest.raises(LaunchError, match="secrets"):
        create(tmp_path, skills, "../secrets")


def test_a_member_declaring_a_skill_with_nowhere_to_read_it_from_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(LaunchError, match="rebaser"):
        create(tmp_path, None, "rebaser")


def test_a_member_declaring_none_needs_no_skills_directory(tmp_path: Path) -> None:
    directory = create(tmp_path, None)

    assert list((directory / piconfig.SKILLS_DIR).iterdir()) == []
