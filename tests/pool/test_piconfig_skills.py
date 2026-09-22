"""A member's skills: copied into its generated directory, links resolved.

The operator's own skills directory is a mix of real directories and links
into other checkouts, and the sandbox hides the whole of the operator's home,
so neither a bind nor an absolute path would reach them. Each declared skill
is copied instead, whole and dereferenced, so the generated directory means
the same thing inside the sandbox as outside it, and a skill edited on the
host cannot change under a member that is already running.
"""

from __future__ import annotations

from pathlib import Path

from specflo.pool import piconfig

from .test_piconfig import ACCOUNTS, NO_TRAIN_MEMBER

SKILL = "---\nname: {name}\n---\n\nThe {name} skill.\n"


def operator_skills(tmp_path: Path) -> Path:
    """A skills directory in the shape the operator's is: one real skill, one
    that is a link to a checkout elsewhere, and one nobody declares."""
    elsewhere = tmp_path / "checkouts" / "tavily-cli"
    elsewhere.mkdir(parents=True)
    (elsewhere / "SKILL.md").write_text(SKILL.format(name="tavily-cli"), encoding="utf-8")
    (elsewhere / "reference.md").write_text("What the CLI takes.\n", encoding="utf-8")

    skills = tmp_path / "agent" / "skills"
    (skills / "rebaser").mkdir(parents=True)
    (skills / "rebaser" / "SKILL.md").write_text(SKILL.format(name="rebaser"), encoding="utf-8")
    (skills / "undeclared").mkdir()
    (skills / "undeclared" / "SKILL.md").write_text(
        SKILL.format(name="undeclared"), encoding="utf-8"
    )
    (skills / "tavily-cli").symlink_to(elsewhere)
    return skills


def generated(tmp_path: Path, *names: str) -> Path:
    return piconfig.create(
        tmp_path / "generated", NO_TRAIN_MEMBER, ACCOUNTS,
        skills=names, skills_from=operator_skills(tmp_path),
    )


def test_a_declared_skill_is_a_real_file_under_the_generated_directory(
    tmp_path: Path,
) -> None:
    directory = generated(tmp_path, "rebaser")

    definition = directory / piconfig.SKILLS_DIR / "rebaser" / "SKILL.md"
    assert definition.is_file() and not definition.is_symlink()
    assert definition.read_text(encoding="utf-8") == SKILL.format(name="rebaser")


def test_a_skill_the_operator_keeps_as_a_link_is_copied_whole(tmp_path: Path) -> None:
    directory = generated(tmp_path, "tavily-cli")

    copied = directory / piconfig.SKILLS_DIR / "tavily-cli"
    assert copied.is_dir() and not copied.is_symlink()
    assert (copied / "SKILL.md").is_file() and not (copied / "SKILL.md").is_symlink()
    assert (copied / "reference.md").read_text(encoding="utf-8") == "What the CLI takes.\n"


def test_a_link_inside_a_skill_is_resolved_too(tmp_path: Path) -> None:
    skills = operator_skills(tmp_path)
    (tmp_path / "shared.md").write_text("Shared between skills.\n", encoding="utf-8")
    (skills / "rebaser" / "shared.md").symlink_to(tmp_path / "shared.md")

    directory = piconfig.create(
        tmp_path / "generated", NO_TRAIN_MEMBER, ACCOUNTS,
        skills=("rebaser",), skills_from=skills,
    )

    copied = directory / piconfig.SKILLS_DIR / "rebaser" / "shared.md"
    assert copied.is_file() and not copied.is_symlink()
    assert copied.read_text(encoding="utf-8") == "Shared between skills.\n"


def test_the_copy_does_not_change_when_the_source_is_edited(tmp_path: Path) -> None:
    skills = operator_skills(tmp_path)
    directory = piconfig.create(
        tmp_path / "generated", NO_TRAIN_MEMBER, ACCOUNTS,
        skills=("rebaser",), skills_from=skills,
    )

    (skills / "rebaser" / "SKILL.md").write_text("Do something else.\n", encoding="utf-8")

    copied = directory / piconfig.SKILLS_DIR / "rebaser" / "SKILL.md"
    assert copied.read_text(encoding="utf-8") == SKILL.format(name="rebaser")


def test_a_skill_the_member_does_not_declare_is_not_there(tmp_path: Path) -> None:
    directory = generated(tmp_path, "rebaser")

    assert sorted(p.name for p in (directory / piconfig.SKILLS_DIR).iterdir()) == ["rebaser"]


def test_a_member_declaring_none_gets_an_empty_skills_directory(tmp_path: Path) -> None:
    # The directory says what the member's skills are, and for this member
    # that is nothing at all.
    directory = generated(tmp_path)

    assert list((directory / piconfig.SKILLS_DIR).iterdir()) == []


def test_the_operators_skills_are_under_the_directory_the_variable_names() -> None:
    environ = {"HOME": "/home/pool", "PI_CODING_AGENT_DIR": "/elsewhere/agent"}

    assert piconfig.operator_skills(environ) == Path("/elsewhere/agent/skills")


def test_the_operators_skills_fall_back_to_the_home_rooted_directory() -> None:
    assert piconfig.operator_skills({"HOME": "/home/pool"}) == Path(
        "/home/pool/.pi/agent/skills"
    )
