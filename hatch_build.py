# hatch_build.py - build-time completeness guard for the repo-root skills/.
#
# The actual inclusion of skills/ into the wheel is done by the one force-include
# line in pyproject.toml ([tool.hatch.build.targets.wheel.force-include]). This
# hook adds no force-include of its own -- doing so would map the same files to
# the same specflo/_repo_skills/<name> paths and raise a duplicate-path build
# error. Its sole job is to fail the build loudly if a required repo skill has
# been dropped or emptied, so a stray delete or ignore rule can't silently ship a
# wheel that is missing a workflow skill.
#
# Self-contained: not an agentsquire dependency. The hatchling import is guarded
# so the completeness check stays importable by the test suite, which does not
# install hatchling.
from pathlib import Path

# The workflow skills that MUST be present in the build. A dropped skill fails
# the build; extra skills are allowed (this is a floor, not a ceiling).
EXPECTED_SKILLS = frozenset(
    {"auto", "brainstorm", "execute", "plan", "research", "shelve", "spec"}
)


def check_skills_complete(skills_dir: Path) -> list[str]:
    """Return the sorted skill names in ``skills_dir``, or raise if incomplete.

    A skill counts as present only if its directory contains a ``SKILL.md``.
    Raises ``ValueError`` if ``skills_dir`` is missing or any EXPECTED skill is
    absent -- the message names what is missing so the build failure is legible.
    """
    skills_dir = Path(skills_dir)
    if not skills_dir.is_dir():
        raise ValueError(f"hatch_build.py: repo-root skills/ is missing at {skills_dir}")
    found = {
        d.name
        for d in skills_dir.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    }
    missing = sorted(EXPECTED_SKILLS - found)
    if missing:
        raise ValueError(
            "hatch_build.py: repo-root skills/ is missing required skills: "
            + ", ".join(missing)
        )
    return sorted(found)


try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ModuleNotFoundError:
    # hatchling is only present in the build environment; when this module is
    # imported by the test suite the check function above is all that's needed.
    pass
else:

    class CustomBuildHook(BuildHookInterface):
        def initialize(self, version, build_data):
            check_skills_complete(Path(self.root) / "skills")
