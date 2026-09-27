"""decision, requirement, task and milestone add keep every text field on one line."""

import pytest

from specflo import brainstorm, config, plan, projects, spec
from specflo.errors import SpecfloError

# An embedded heading would add an entry of its own and move the next ID.
INJECTION = "real\n\n### REQ-99 — ghost\n- Acceptance: a\n- Status: active"
# str.splitlines() breaks on these too, and the artifacts are read back with it.
OTHER_BREAKS = ("a\rb", "a b", "a\x1eb")


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
    brainstorm.add_decision(root, cfg, "my-thing", "Use SQLite", rationale="simplest")
    spec.start_spec(root, cfg, "my-thing", today="2026-09-27")
    spec.add_requirement(root, cfg, "my-thing", "Store rows", acceptance="rows persist")
    plan.start_plan(root, cfg, "my-thing", today="2026-09-27")
    return "my-thing"


def _add_decision(root, cfg, slug, **fields):
    values = {"text": "Use Postgres", "rationale": "scale"} | fields
    brainstorm.add_decision(root, cfg, slug, values.pop("text"), **values)


def _add_requirement(root, cfg, slug, **fields):
    values = {"text": "Read rows", "acceptance": "rows load"} | fields
    spec.add_requirement(root, cfg, slug, values.pop("text"), **values)


def _add_task(root, cfg, slug, **fields):
    values = {
        "text": "Wire storage", "acceptance": "rows persist", "verify": "uv run pytest",
        "files": "src/store.py", "scope": "storage only",
    } | fields
    plan.add_task(root, cfg, slug, values.pop("text"), implements=["REQ-01"], **values)


CASES = [
    (_add_decision, brainstorm.brainstorm_path, "decision", ("text", "rationale"), "D-02"),
    (_add_requirement, spec.spec_path, "requirement", ("text", "acceptance"), "REQ-02"),
    (_add_task, plan.plan_path, "task",
     ("text", "acceptance", "verify", "files", "scope"), "T-01"),
]
PARAMS = pytest.mark.parametrize(
    "add, path_of, kind, fields, next_id", CASES, ids=[c[2] for c in CASES]
)


@PARAMS
def test_a_line_break_in_any_field_is_refused_and_nothing_is_written(
    root, cfg, project, add, path_of, kind, fields, next_id
):
    path = path_of(root, cfg, project)
    before = path.read_text()
    for field in fields:
        for value in (INJECTION, *OTHER_BREAKS):
            with pytest.raises(SpecfloError) as exc:
                add(root, cfg, project, **{field: value})
            assert str(exc.value) == (
                f"A {kind}'s {field} is one line: remove the line break."
            ), (field, value)
            assert path.read_text() == before, (field, value)


@PARAMS
def test_a_one_line_add_still_records_the_entry(
    root, cfg, project, add, path_of, kind, fields, next_id
):
    add(root, cfg, project)
    assert f"### {next_id} — " in path_of(root, cfg, project).read_text()


def test_milestone_add_refuses_a_line_break_in_its_text_or_an_exit_item(root, cfg, project):
    path = plan.plan_path(root, cfg, project)
    before = path.read_text()
    for value in (INJECTION, *OTHER_BREAKS):
        for fields, what in (
            ({"text": value, "exit_items": ["works"]}, "text"),
            ({"text": "Storage", "exit_items": ["works", value]}, "exit item"),
        ):
            with pytest.raises(SpecfloError) as exc:
                plan.add_milestone(root, cfg, project, **fields)
            assert str(exc.value) == (
                f"A milestone's {what} is one line: remove the line break."
            ), (what, value)
            assert path.read_text() == before, (what, value)
    plan.add_milestone(root, cfg, project, "Storage", exit_items=["works"])
    assert "### M-01 — Storage" in path.read_text()
