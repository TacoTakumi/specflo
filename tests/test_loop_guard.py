"""decision add, requirement add and task add refuse text an active entry already holds.

A model that loops adds the same entry again and again, so the refusal tells it
that it may be in a loop and what to do next.
"""

import pytest

from specflo import brainstorm, brief, config, plan, projects, spec
from specflo.errors import SpecfloError


@pytest.fixture
def root(tmp_path):
    config.init_config(tmp_path)
    return tmp_path


@pytest.fixture
def cfg(root):
    return config.load_config(root)


@pytest.fixture
def project(root, cfg):
    projects.create_project(root, cfg, "My Thing", created="2026-09-27")
    brainstorm.start_brainstorm(root, cfg, "my-thing", today="2026-09-27")
    spec.start_spec(root, cfg, "my-thing", today="2026-09-27")
    spec.add_requirement(root, cfg, "my-thing", "Base", acceptance="base works")
    plan.start_plan(root, cfg, "my-thing", today="2026-09-27")
    return "my-thing"


def _decision(root, cfg, slug, text, supersedes=None):
    return brainstorm.add_decision(root, cfg, slug, text, rationale="why",
                                   supersedes=supersedes).id


def _requirement(root, cfg, slug, text, supersedes=None):
    return spec.add_requirement(root, cfg, slug, text, acceptance="it holds",
                                supersedes=supersedes).id


def _task(root, cfg, slug, text, supersedes=None):
    return plan.add_task(root, cfg, slug, text, acceptance="it holds", verify="a test",
                         implements=["REQ-01"], supersedes=supersedes).id


CASES = [
    (_decision, brainstorm.brainstorm_path, "decision", "brainstorm"),
    (_requirement, spec.spec_path, "requirement", "spec"),
    (_task, plan.plan_path, "task", "plan"),
]
PARAMS = pytest.mark.parametrize("add, path_of, kind, doc", CASES, ids=[c[2] for c in CASES])


@PARAMS
def test_text_an_active_entry_holds_is_refused_and_nothing_is_written(
    root, cfg, project, add, path_of, kind, doc
):
    first = add(root, cfg, project, "Use SQLite for storage")
    path = path_of(root, cfg, project)
    before = path.read_text()
    for again in ("Use SQLite for storage", "  use sqlite   FOR storage "):
        with pytest.raises(SpecfloError) as exc:
            add(root, cfg, project, again)
        message = str(exc.value)
        assert f"{first} already records this {kind}" in message
        assert "you may be in a loop" in message
        assert f"`specflo doc show {doc}`" in message
        assert f"`--supersedes {first}`" in message
        assert path.read_text() == before


@PARAMS
def test_superseding_the_matching_entry_may_keep_its_text(
    root, cfg, project, add, path_of, kind, doc
):
    first = add(root, cfg, project, "Use SQLite for storage")
    second = add(root, cfg, project, "Use SQLite for storage", supersedes=first)
    assert second != first
    with pytest.raises(SpecfloError) as exc:  # the replacement is active now
        add(root, cfg, project, "Use SQLite for storage")
    assert f"{second} already records this {kind}" in str(exc.value)


@PARAMS
def test_the_text_of_a_superseded_entry_may_be_added_again(
    root, cfg, project, add, path_of, kind, doc
):
    first = add(root, cfg, project, "Use SQLite for storage")
    add(root, cfg, project, "Use Postgres for storage", supersedes=first)
    third = add(root, cfg, project, "Use SQLite for storage")
    assert f"### {third} — Use SQLite for storage" in path_of(root, cfg, project).read_text()


def test_a_brief_with_the_same_check_twice_still_moves_up(root, cfg):
    projects.create_project(root, cfg, "Quick One", created="2026-09-27")
    brief.start_brief(root, cfg, "quick-one", today="2026-09-27")
    path = brief.brief_path(root, cfg, "quick-one")
    doc = path.read_text()
    path.write_text(doc.replace(
        "## Done when\n",
        "## Done when\n- the tests pass\n- The tests  pass\n",
    ))
    brief.seed_fast_documents(root, cfg, "quick-one")
    spec_doc = spec.spec_path(root, cfg, "quick-one").read_text()
    assert spec.active_requirement_ids(spec_doc) == ["REQ-01"]
