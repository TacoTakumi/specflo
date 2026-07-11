"""specflo declares the agentsquire.skills entry-point marker.

One pyproject line registers specflo under the ``agentsquire.skills`` entry-point
group, marking it as a skill-carrying package. Nothing reads it today -- it is a
reserved marker that changes no runtime behavior -- so these tests only assert
that the installed distribution exposes the entry point and that it resolves to
the importable specflo package.
"""

from importlib.metadata import entry_points


def test_agentsquire_skills_entry_point_is_declared():
    marks = {
        ep.name: ep.value for ep in entry_points(group="agentsquire.skills")
    }
    assert marks.get("specflo") == "specflo"


def test_entry_point_resolves_to_the_specflo_package():
    specflo_eps = [
        ep for ep in entry_points(group="agentsquire.skills") if ep.name == "specflo"
    ]
    assert len(specflo_eps) == 1
    # value "specflo" (no attribute) loads the module itself.
    assert specflo_eps[0].load().__name__ == "specflo"
