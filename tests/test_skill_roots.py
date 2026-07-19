"""The two skill roots stay disjoint and the zero-arg union enumerates the 7.

specflo carries no package-data skills -- Root A (``src/specflo/skills/``) is an
empty marker directory; the 7 workflow skills live at repo-root ``skills/`` and
resolve as agentsquire Root B. This proves the default two-root union enumerates
exactly those 7 skills, once each, with no ``DuplicateSkillError``, resolved from
repo-root ``skills/`` in the editable dev checkout -- driven through the zero-arg
``default_source`` with no ``source=`` override.
"""

from pathlib import Path

import pytest

from agentsquire import verify_skill_roots
from agentsquire.sources import default_source

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_NAMES = ["auto", "brainstorm", "execute", "plan", "research", "shelve", "spec"]


def test_skill_roots_are_disjoint():
    # Raises DuplicateSkillError if Root A and Root B share a name (and
    # FileNotFoundError if the empty Root A marker went missing), so this guards
    # both the disjointness invariant and the marker's presence.
    verify_skill_roots("specflo")


def test_union_enumerates_exactly_the_seven_skills():
    names = [skill.name for skill in default_source("specflo").list_skills()]
    assert sorted(names) == sorted(SKILL_NAMES)
    assert len(names) == len(set(names))  # each exactly once


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_resolves_from_repo_root_in_editable_checkout(name):
    with default_source("specflo").materialize(name) as path:
        assert (path / "SKILL.md").is_file()
        # Root B resolves from repo-root skills/ (marker-walk), not _repo_skills.
        assert Path(path).resolve() == (REPO_ROOT / "skills" / name).resolve()
