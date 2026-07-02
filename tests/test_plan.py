import pytest

from specflo import brainstorm, config, markdown, plan, projects, spec
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
    projects.create_project(root, cfg, "My Thing", created="2026-06-15")
    return "my-thing"


def _ppath(root, cfg, project):
    return plan.plan_path(root, cfg, project)


def test_start_creates_plan_with_frontmatter(root, cfg, project):
    path, created = plan.start_plan(root, cfg, project, today="2026-06-22")
    assert created is True
    assert path == root / "docs" / "projects" / "my-thing" / "plan.md"
    text = path.read_text()
    assert "project: my-thing" in text
    assert "phase: plan" in text
    assert "status: draft" in text
    assert "created: 2026-06-22" in text
    assert "# Plan: My Thing" in text
    assert "## Approach" in text
    assert "## Tasks" in text
    assert "## Open questions" in text


def test_start_is_idempotent_and_does_not_clobber(root, cfg, project):
    path, first = plan.start_plan(root, cfg, project, today="2026-06-22")
    path.write_text(path.read_text() + "\nUSER MARKER\n")
    path_again, second = plan.start_plan(root, cfg, project, today="2026-06-23")
    assert first is True and second is False
    assert path_again == path
    assert "USER MARKER" in path.read_text()


def test_start_on_missing_project_raises(root, cfg):
    with pytest.raises(SpecfloError):
        plan.start_plan(root, cfg, "ghost")


def _spec_with_reqs(root, cfg, project, n=2):
    spec.start_spec(root, cfg, project, today="2026-06-22")
    for i in range(n):
        spec.add_requirement(root, cfg, project, f"req {i}", acceptance="ok", today="2026-06-22")


def test_add_task_assigns_sequential_ids_and_records_fields(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=2)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    t1 = plan.add_task(root, cfg, project, "build the parser",
                       acceptance="parses a task block", verify="uv run pytest",
                       implements=["REQ-01"], today="2026-06-22")
    t2 = plan.add_task(root, cfg, project, "wire the CLI",
                       acceptance="command exits 0", verify="uv run specflo task list",
                       implements=["REQ-01", "REQ-02"], depends_on=["T-01"], today="2026-06-22")
    assert t1.id == "T-01" and t2.id == "T-02"
    text = _ppath(root, cfg, project).read_text()
    assert "### T-01 — build the parser" in text
    assert "- Acceptance: parses a task block" in text
    assert "- Verify: uv run pytest" in text
    assert "- Implements: REQ-01, REQ-02" in text
    assert "- Depends on: T-01" in text
    assert "- Progress: pending" in text
    assert "- Status: active" in text


def test_add_task_requires_at_least_one_requirement(root, cfg, project):
    _spec_with_reqs(root, cfg, project)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    with pytest.raises(SpecfloError):
        plan.add_task(root, cfg, project, "orphan", acceptance="a", verify="v", implements=[])


def test_add_task_rejects_unknown_or_superseded_requirement(root, cfg, project):
    spec.start_spec(root, cfg, project, today="2026-06-22")
    spec.add_requirement(root, cfg, project, "old", acceptance="a", today="2026-06-22")          # REQ-01
    spec.add_requirement(root, cfg, project, "new", acceptance="b",
                         supersedes="REQ-01", today="2026-06-22")                                  # REQ-02
    plan.start_plan(root, cfg, project, today="2026-06-22")
    with pytest.raises(SpecfloError):
        plan.add_task(root, cfg, project, "x", acceptance="a", verify="v", implements=["REQ-99"])
    with pytest.raises(SpecfloError):  # superseded REQ is not active
        plan.add_task(root, cfg, project, "x", acceptance="a", verify="v", implements=["REQ-01"])


def test_add_task_validates_dependency_and_supersede_targets(root, cfg, project):
    _spec_with_reqs(root, cfg, project)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "first", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")                                       # T-01
    with pytest.raises(SpecfloError):
        plan.add_task(root, cfg, project, "bad dep", acceptance="a", verify="v",
                      implements=["REQ-01"], depends_on=["T-99"])
    with pytest.raises(SpecfloError):
        plan.add_task(root, cfg, project, "bad sup", acceptance="a", verify="v",
                      implements=["REQ-01"], supersedes="T-99")
    t = plan.add_task(root, cfg, project, "replacement", acceptance="a", verify="v",
                      implements=["REQ-01"], supersedes="T-01", today="2026-06-22")                # T-02
    text = _ppath(root, cfg, project).read_text()
    assert t.id == "T-02"
    assert "- Status: superseded by T-02" in text
    assert "- Supersedes: T-01" in text


def test_supersede_resets_progress_and_writes_bidirectional_link(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "old", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")                       # T-01
    plan.start_task(root, cfg, project, "T-01")                                    # -> in_progress
    plan.add_task(root, cfg, project, "new", acceptance="b", verify="v",
                  implements=["REQ-01"], supersedes="T-01", today="2026-06-22")    # T-02
    text = _ppath(root, cfg, project).read_text()
    t1 = {t.id: t for t in
          plan.list_tasks(root, cfg, project, include_superseded=True)}["T-01"]
    assert t1.progress == "pending"          # REQ-06: reset from in_progress
    assert t1.superseded_by == "T-02"        # REQ-07: forward link parses
    assert "- Superseded by: T-02" in text   # REQ-07: explicit field written
    assert "- Supersedes: T-01" in text      # REQ-07: back link on the new task


def test_add_task_without_start_raises(root, cfg, project):
    with pytest.raises(SpecfloError):
        plan.add_task(root, cfg, project, "early", acceptance="a", verify="v", implements=["REQ-01"])


def _good_plan(root, cfg, project):
    """A plan with full bidirectional coverage of a 2-requirement spec."""
    _spec_with_reqs(root, cfg, project, n=2)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "task a", acceptance="a passes", verify="uv run pytest",
                  implements=["REQ-01"], today="2026-06-22")                                  # T-01
    plan.add_task(root, cfg, project, "task b", acceptance="b passes", verify="uv run pytest",
                  implements=["REQ-02"], depends_on=["T-01"], today="2026-06-22")             # T-02


def test_validate_passes_a_complete_plan(root, cfg, project):
    _good_plan(root, cfg, project)
    assert plan.validate_plan(root, cfg, project) == []


def test_validate_flags_missing_file(root, cfg, project):
    assert any("not found" in i for i in plan.validate_plan(root, cfg, project))


def test_validate_flags_no_tasks(root, cfg, project):
    _spec_with_reqs(root, cfg, project)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    assert any("no tasks" in i for i in plan.validate_plan(root, cfg, project))


def test_validate_flags_uncovered_requirement(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=2)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "only a", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")  # REQ-02 left uncovered
    issues = plan.validate_plan(root, cfg, project)
    assert any("REQ-02" in i and "not implemented" in i for i in issues)


def test_validate_flags_missing_acceptance_and_verify(root, cfg, project):
    _good_plan(root, cfg, project)
    path = _ppath(root, cfg, project)
    path.write_text(path.read_text()
                    .replace("- Acceptance: a passes", "- Acceptance: ")
                    .replace("- Verify: uv run pytest", "- Verify: ", 1))
    issues = plan.validate_plan(root, cfg, project)
    assert any("acceptance" in i.lower() for i in issues)
    assert any("verification" in i.lower() for i in issues)


def test_validate_flags_dangling_dependency_and_cycle(root, cfg, project):
    _good_plan(root, cfg, project)
    path = _ppath(root, cfg, project)
    # make T-01 depend on T-02 -> cycle (T-01 -> T-02 -> T-01)
    text = path.read_text().replace(
        "### T-01 — task a\n- Acceptance: a passes\n- Verify: uv run pytest\n- Implements: REQ-01\n",
        "### T-01 — task a\n- Acceptance: a passes\n- Verify: uv run pytest\n- Implements: REQ-01\n- Depends on: T-02\n",
    )
    path.write_text(text)
    assert any("cycle" in i for i in plan.validate_plan(root, cfg, project))


def test_validate_ignores_superseded_tasks(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "old", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")                                  # T-01
    plan.add_task(root, cfg, project, "new", acceptance="b", verify="v",
                  implements=["REQ-01"], supersedes="T-01", today="2026-06-22")               # T-02
    # blank the superseded entry's acceptance — validate must still pass
    path = _ppath(root, cfg, project)
    path.write_text(path.read_text().replace("- Acceptance: a", "- Acceptance: "))
    assert plan.validate_plan(root, cfg, project) == []


def test_plan_warnings_flags_scope_reduction_vocab(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "ship a stub for now",
                  acceptance="returns a value", verify="v",
                  implements=["REQ-01"], today="2026-06-22")
    warnings = plan.plan_warnings(root, cfg, project)
    assert any("stub" in w for w in warnings)
    assert any("for now" in w for w in warnings)


def test_plan_warnings_empty_on_clean_plan(root, cfg, project):
    _good_plan(root, cfg, project)
    assert plan.plan_warnings(root, cfg, project) == []


def test_complete_plan_flips_status_and_leaves_tasks_untouched(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "t", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")
    plan.complete_plan(root, cfg, project, today="2026-06-24")
    text = _ppath(root, cfg, project).read_text()
    assert "status: complete" in text
    assert "status: draft" not in text
    assert "updated: 2026-06-24" in text
    assert "- Status: active" in text   # task entry untouched


def test_complete_plan_without_file_raises(root, cfg, project):
    with pytest.raises(SpecfloError):
        plan.complete_plan(root, cfg, project)


def test_progress_transitions(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "t", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")  # T-01
    plan.start_task(root, cfg, project, "T-01")
    assert "- Progress: in_progress" in _ppath(root, cfg, project).read_text()
    plan.done_task(root, cfg, project, "T-01")
    assert "- Progress: done" in _ppath(root, cfg, project).read_text()
    plan.block_task(root, cfg, project, "T-01", reason="waiting on API")
    text = _ppath(root, cfg, project).read_text()
    assert "- Progress: blocked" in text
    assert "- Blocked: waiting on API" in text
    plan.reopen_task(root, cfg, project, "T-01")
    text = _ppath(root, cfg, project).read_text()
    assert "- Progress: pending" in text
    assert "Blocked" not in text  # cleared on reopen


def test_done_requires_in_progress(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "t", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")   # T-01 pending
    with pytest.raises(SpecfloError):
        plan.done_task(root, cfg, project, "T-01")             # pending -> done refused
    plan.start_task(root, cfg, project, "T-01")
    plan.done_task(root, cfg, project, "T-01")                 # in_progress -> done ok
    assert "- Progress: done" in _ppath(root, cfg, project).read_text()


def test_transition_on_unknown_or_superseded_task_raises(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "old", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")                                # T-01
    plan.add_task(root, cfg, project, "new", acceptance="b", verify="v",
                  implements=["REQ-01"], supersedes="T-01", today="2026-06-22")             # T-02
    with pytest.raises(SpecfloError):
        plan.start_task(root, cfg, project, "T-99")
    with pytest.raises(SpecfloError):
        plan.start_task(root, cfg, project, "T-01")  # superseded -> frozen


def test_plan_progress_is_dependency_aware(root, cfg, project):
    _good_plan(root, cfg, project)  # T-01, T-02 (T-02 depends on T-01)
    prog = plan.plan_progress(root, cfg, project)
    assert prog["total"] == 2
    assert prog["by_state"]["pending"] == 2
    assert prog["next_actionable"] == ["T-01"]  # T-02 blocked by its dep
    assert prog["all_done"] is False
    plan.start_task(root, cfg, project, "T-01")
    plan.done_task(root, cfg, project, "T-01")
    prog = plan.plan_progress(root, cfg, project)
    assert prog["done"] == 1
    assert prog["next_actionable"] == ["T-02"]  # dep now satisfied
    plan.start_task(root, cfg, project, "T-02")
    plan.done_task(root, cfg, project, "T-02")
    assert plan.plan_progress(root, cfg, project)["all_done"] is True


def test_plan_progress_zero_when_no_plan(root, cfg, project):
    prog = plan.plan_progress(root, cfg, project)
    assert prog["total"] == 0 and prog["all_done"] is False


def test_list_tasks_hides_superseded_by_default(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "old", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")                                # T-01
    plan.add_task(root, cfg, project, "new", acceptance="b", verify="v",
                  implements=["REQ-01"], supersedes="T-01", today="2026-06-22")             # T-02
    assert [t.id for t in plan.list_tasks(root, cfg, project)] == ["T-02"]
    assert [t.id for t in plan.list_tasks(root, cfg, project, include_superseded=True)] == ["T-01", "T-02"]


def test_parser_recognizes_superseded_via_new_field_and_legacy_status(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "keeper", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")    # T-01 active
    plan.add_task(root, cfg, project, "new-field", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")    # T-02
    plan.add_task(root, cfg, project, "legacy", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")    # T-03
    path = _ppath(root, cfg, project)
    text = path.read_text()
    # T-02 carries ONLY the new bidirectional field; its Status line stays active,
    # so the new field alone must drive superseded detection.
    text = text.replace(
        "### T-02 — new-field\n- Acceptance: a\n- Verify: v\n- Implements: REQ-01\n",
        "### T-02 — new-field\n- Acceptance: a\n- Verify: v\n- Implements: REQ-01\n- Superseded by: T-11\n",
    )
    # T-03 carries only the legacy Status marker.
    text = text.replace(
        "### T-03 — legacy\n- Acceptance: a\n- Verify: v\n- Implements: REQ-01\n- Progress: pending\n- Status: active\n",
        "### T-03 — legacy\n- Acceptance: a\n- Verify: v\n- Implements: REQ-01\n- Progress: pending\n- Status: superseded by T-11\n",
    )
    path.write_text(text)

    all_tasks = {t.id: t for t in plan.list_tasks(root, cfg, project, include_superseded=True)}
    assert all_tasks["T-02"].status == "superseded"
    assert all_tasks["T-02"].superseded_by == "T-11"
    assert all_tasks["T-03"].status == "superseded"
    assert all_tasks["T-03"].superseded_by == "T-11"

    assert [t.id for t in plan.list_tasks(root, cfg, project)] == ["T-01"]  # both excluded
    assert plan.plan_progress(root, cfg, project)["next_actionable"] == ["T-01"]


def _raw_task_entry(tid, deps=None, status="active", implements="REQ-01",
                    milestone=None, progress="pending"):
    """A well-formed task entry block for crafting plans with explicit ids."""
    lines = [f"### {tid} — task {tid}", "- Acceptance: a", "- Verify: v",
             f"- Implements: {implements}"]
    if deps:
        lines.append(f"- Depends on: {', '.join(deps)}")
    if milestone:
        lines.append(f"- Milestone: {milestone}")
    lines += [f"- Progress: {progress}", f"- Status: {status}"]
    return "\n".join(lines) + "\n"


def _plan_with_entries(root, cfg, project, entries):
    plan.start_plan(root, cfg, project, today="2026-06-22")
    path = _ppath(root, cfg, project)
    doc = path.read_text()
    for entry in entries:
        doc = markdown.append_to_section(doc, "## Tasks", entry)
    path.write_text(doc)
    return path


def test_rewire_dependency_repoints_active_dependents_only(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    _plan_with_entries(root, cfg, project, [
        _raw_task_entry("T-04"),
        _raw_task_entry("T-05", deps=["T-04", "T-09"]),
        _raw_task_entry("T-06", deps=["T-04"]),
        _raw_task_entry("T-07", deps=["T-09"]),                              # not on T-04
        _raw_task_entry("T-08", deps=["T-04"], status="superseded by T-99"),  # superseded
        _raw_task_entry("T-09"),
        _raw_task_entry("T-11"),
    ])
    changed = plan.rewire_dependency(root, cfg, project, "T-04", "T-11")
    assert changed == ["T-05", "T-06"]

    tasks = {t.id: t for t in plan.list_tasks(root, cfg, project, include_superseded=True)}
    assert tasks["T-05"].depends_on == ["T-11", "T-09"]   # T-04 -> T-11, T-09 kept, order kept
    assert tasks["T-06"].depends_on == ["T-11"]
    assert tasks["T-07"].depends_on == ["T-09"]           # non-dependent untouched
    assert tasks["T-08"].depends_on == ["T-04"]           # superseded untouched


def test_rewire_dependency_noop_leaves_plan_byte_identical(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    path = _plan_with_entries(root, cfg, project, [
        _raw_task_entry("T-01"),
        _raw_task_entry("T-02", deps=["T-01"]),
        _raw_task_entry("T-03"),
    ])
    before = path.read_bytes()
    # T-03 is a valid target but nothing depends on it -> no-op, byte-identical.
    assert plan.rewire_dependency(root, cfg, project, "T-03", "T-01") == []
    assert path.read_bytes() == before


def test_rewire_dependency_validates_inputs_and_stays_inert(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    path = _plan_with_entries(root, cfg, project, [
        _raw_task_entry("T-01"),
        _raw_task_entry("T-02", deps=["T-01"]),
        _raw_task_entry("T-03", status="superseded by T-99"),
    ])
    before = path.read_bytes()
    with pytest.raises(SpecfloError):  # --to nonexistent
        plan.rewire_dependency(root, cfg, project, "T-01", "T-99")
    with pytest.raises(SpecfloError):  # --to a superseded task
        plan.rewire_dependency(root, cfg, project, "T-01", "T-03")
    with pytest.raises(SpecfloError):  # --to equals --from
        plan.rewire_dependency(root, cfg, project, "T-01", "T-01")
    with pytest.raises(SpecfloError):  # --from nonexistent
        plan.rewire_dependency(root, cfg, project, "T-99", "T-01")
    assert path.read_bytes() == before  # byte-identical after every rejection


def test_rewire_dependency_dedupes_existing_target(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    _plan_with_entries(root, cfg, project, [
        _raw_task_entry("T-04"),
        _raw_task_entry("T-05", deps=["T-04", "T-11"]),  # already lists the target
        _raw_task_entry("T-11"),
    ])
    assert plan.rewire_dependency(root, cfg, project, "T-04", "T-11") == ["T-05"]
    tasks = {t.id: t for t in plan.list_tasks(root, cfg, project, include_superseded=True)}
    assert tasks["T-05"].depends_on == ["T-11"]  # single entry, no duplicate


def test_rewire_dependency_refuses_a_cycle_and_stays_inert(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    # T-11 depends on T-05; rewiring T-05's dep T-04 -> T-11 would make T-05 -> T-11 -> T-05.
    path = _plan_with_entries(root, cfg, project, [
        _raw_task_entry("T-04"),
        _raw_task_entry("T-05", deps=["T-04"]),
        _raw_task_entry("T-11", deps=["T-05"]),
    ])
    before = path.read_bytes()
    with pytest.raises(SpecfloError):
        plan.rewire_dependency(root, cfg, project, "T-04", "T-11")
    assert path.read_bytes() == before  # cycle-refused, byte-identical


def test_blocked_on_superseded_detects_and_task_brief_guides(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)
    _plan_with_entries(root, cfg, project, [
        _raw_task_entry("T-04", status="superseded by T-11"),
        _raw_task_entry("T-05", deps=["T-04"]),   # pending, blocked by superseded T-04
    ])
    blocks = plan.blocked_on_superseded(root, cfg, project)
    assert blocks == [{"blocked": "T-05", "dependency": "T-04", "superseded_by": "T-11"}]
    with pytest.raises(SpecfloError) as exc:
        plan.task_brief(root, cfg, project)       # no id -> nothing actionable
    msg = str(exc.value)
    for token in ("T-05", "T-04", "T-11", "specflo task rewire --from T-04 --to T-11"):
        assert token in msg


def test_task_brief_assembles_task_reqs_and_constraints(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=2)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    ppath = _ppath(root, cfg, project)
    ppath.write_text(ppath.read_text().replace(
        "## Global constraints\n"
        "<!-- optional; project-wide invariants copied verbatim from the spec,"
        " implicitly part of every task -->",
        "## Global constraints\n- Python 3.12; use uv."))
    plan.add_task(root, cfg, project, "build a", acceptance="a works",
                  verify="uv run pytest", implements=["REQ-01"], today="2026-06-22")  # T-01
    brief = plan.task_brief(root, cfg, project, "T-01")
    assert brief["task"]["id"] == "T-01"
    assert brief["task"]["acceptance"] == "a works"
    assert brief["requirements"][0]["id"] == "REQ-01"
    assert "REQ-01" in brief["requirements"][0]["section"]
    assert "Python 3.12" in brief["global_constraints"]


def test_task_brief_defaults_to_first_next_actionable(root, cfg, project):
    _good_plan(root, cfg, project)   # T-01, then T-02 (depends on T-01)
    brief = plan.task_brief(root, cfg, project)   # no id -> first actionable
    assert brief["task"]["id"] == "T-01"          # T-02 blocked by T-01


def test_task_brief_unknown_task_raises(root, cfg, project):
    _good_plan(root, cfg, project)
    with pytest.raises(SpecfloError):
        plan.task_brief(root, cfg, project, "T-99")


def test_reconcile_requires_all_tasks_done(root, cfg, project):
    _good_plan(root, cfg, project)   # T-01, T-02 both pending
    assert any("not all tasks are done" in i
               for i in plan.reconcile_issues(root, cfg, project))
    for tid in ("T-01", "T-02"):
        plan.start_task(root, cfg, project, tid)
        plan.done_task(root, cfg, project, tid)
    assert plan.reconcile_issues(root, cfg, project) == []


def test_reconcile_surfaces_coverage_issues(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=2)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    plan.add_task(root, cfg, project, "only a", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")   # REQ-02 uncovered
    assert any("REQ-02" in i for i in plan.reconcile_issues(root, cfg, project))


# --- Milestones (T-01) --------------------------------------------------------


def _started_plan(root, cfg, project, n_reqs=1):
    """A spec with *n_reqs* requirements and a started (empty) plan.md."""
    _spec_with_reqs(root, cfg, project, n=n_reqs)
    plan.start_plan(root, cfg, project, today="2026-06-22")


def test_milestone_add_creates_section_and_exit_block(root, cfg, project):
    _started_plan(root, cfg, project)
    m = plan.add_milestone(root, cfg, project, "Auth works",
                           exit_items=["login succeeds", "logout clears session"],
                           today="2026-06-22")
    assert m.id == "M-01"
    assert m.exit_items == ["login succeeds", "logout clears session"]
    text = _ppath(root, cfg, project).read_text()
    assert "## Milestones" in text
    # the new section is inserted immediately before ## Tasks
    assert text.index("## Milestones") < text.index("## Tasks")
    assert "### M-01 — Auth works" in text
    assert "- Exit:" in text
    assert "  - login succeeds" in text
    assert "  - logout clears session" in text


def test_milestone_add_mints_sequential_ids_and_round_trips(root, cfg, project):
    _started_plan(root, cfg, project)
    plan.add_milestone(root, cfg, project, "First", exit_items=["a"], today="2026-06-22")
    m2 = plan.add_milestone(root, cfg, project, "Second", exit_items=["b", "c"],
                            today="2026-06-22")
    assert m2.id == "M-02"
    doc = _ppath(root, cfg, project).read_text()
    parsed = plan._parse_milestones(doc)
    assert [ms.id for ms in parsed] == ["M-01", "M-02"]        # document order
    assert parsed[0].title == "First" and parsed[0].exit_items == ["a"]
    assert parsed[1].title == "Second" and parsed[1].exit_items == ["b", "c"]


def test_milestone_add_rejects_zero_exit_items(root, cfg, project):
    _started_plan(root, cfg, project)
    with pytest.raises(SpecfloError):
        plan.add_milestone(root, cfg, project, "No exit", exit_items=[])
    with pytest.raises(SpecfloError):  # whitespace-only items collapse to zero
        plan.add_milestone(root, cfg, project, "Blank exit", exit_items=["  ", ""])


def test_milestone_add_without_plan_raises(root, cfg, project):
    _spec_with_reqs(root, cfg, project, n=1)  # spec but no plan.md
    with pytest.raises(SpecfloError):
        plan.add_milestone(root, cfg, project, "x", exit_items=["a"])


def test_milestone_add_leaves_spec_untouched(root, cfg, project):
    _started_plan(root, cfg, project)
    sp = spec.spec_path(root, cfg, project)
    before_bytes = sp.read_bytes()
    before_mtime = sp.stat().st_mtime_ns
    plan.add_milestone(root, cfg, project, "Milestone one", exit_items=["ships"],
                       today="2026-06-22")
    assert sp.read_bytes() == before_bytes       # REQ-01: spec content unchanged
    assert sp.stat().st_mtime_ns == before_mtime  # REQ-01: spec mtime unchanged


def test_milestone_add_creates_section_only_once(root, cfg, project):
    _started_plan(root, cfg, project)
    plan.add_milestone(root, cfg, project, "First", exit_items=["a"], today="2026-06-22")
    plan.add_milestone(root, cfg, project, "Second", exit_items=["b"], today="2026-06-22")
    text = _ppath(root, cfg, project).read_text()
    assert text.count("## Milestones") == 1


# --- Milestone rollup / completion / current (T-02) ---------------------------


def _plan_with_milestones_and_tasks(root, cfg, project, milestones, task_entries, n_reqs=1):
    """Start a plan, author *milestones* [(title, [exit,...]), ...] via add_milestone,
    then append raw *task_entries* (from _raw_task_entry) to ## Tasks. Returns path."""
    _spec_with_reqs(root, cfg, project, n=n_reqs)
    plan.start_plan(root, cfg, project, today="2026-06-22")
    for title, exits in milestones:
        plan.add_milestone(root, cfg, project, title, exit_items=exits, today="2026-06-22")
    path = _ppath(root, cfg, project)
    doc = path.read_text()
    for entry in task_entries:
        doc = markdown.append_to_section(doc, "## Tasks", entry)
    path.write_text(doc)
    return path


def test_task_parses_milestone_membership(root, cfg, project):
    _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["a"])],
        [_raw_task_entry("T-01", milestone="M-01"), _raw_task_entry("T-02")])
    tasks = {t.id: t for t in plan.list_tasks(root, cfg, project)}
    assert tasks["T-01"].milestone == "M-01"
    assert tasks["T-02"].milestone is None


def test_milestone_progress_rolls_up_member_tasks_in_doc_order(root, cfg, project):
    _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["a"]), ("Second", ["b"])],
        [_raw_task_entry("T-01", milestone="M-01"),
         _raw_task_entry("T-02", milestone="M-01"),
         _raw_task_entry("T-03", milestone="M-02")])
    view = plan.milestone_progress(root, cfg, project)
    assert [m["id"] for m in view["milestones"]] == ["M-01", "M-02"]  # document order
    m1, m2 = view["milestones"]
    assert (m1["done"], m1["total"], m1["members"]) == (0, 2, ["T-01", "T-02"])
    assert (m2["done"], m2["total"], m2["members"]) == (0, 1, ["T-03"])
    assert m1["complete"] is False and m2["complete"] is False


def test_milestone_completion_is_derived_and_flips_on_last_task(root, cfg, project):
    _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["a"])],
        [_raw_task_entry("T-01", milestone="M-01"),
         _raw_task_entry("T-02", milestone="M-01")])
    plan.start_task(root, cfg, project, "T-01"); plan.done_task(root, cfg, project, "T-01")
    assert plan.milestone_progress(root, cfg, project)["milestones"][0]["complete"] is False
    plan.start_task(root, cfg, project, "T-02"); plan.done_task(root, cfg, project, "T-02")
    view = plan.milestone_progress(root, cfg, project)
    assert view["milestones"][0]["complete"] is True   # flipped with no extra command
    assert view["milestones"][0]["done"] == 2


def test_current_milestone_is_earliest_incomplete_then_none(root, cfg, project):
    _plan_with_milestones_and_tasks(
        root, cfg, project,
        [("First", ["a"]), ("Second", ["b"]), ("Third", ["c"])],
        [_raw_task_entry("T-01", milestone="M-01"),
         _raw_task_entry("T-02", milestone="M-02"),
         _raw_task_entry("T-03", milestone="M-03")])
    assert plan.milestone_progress(root, cfg, project)["current"] == "M-01"
    plan.start_task(root, cfg, project, "T-01"); plan.done_task(root, cfg, project, "T-01")
    assert plan.milestone_progress(root, cfg, project)["current"] == "M-02"  # M-01 complete
    for tid in ("T-02", "T-03"):
        plan.start_task(root, cfg, project, tid); plan.done_task(root, cfg, project, tid)
    assert plan.milestone_progress(root, cfg, project)["current"] is None    # all complete


def test_milestone_completion_writes_no_persisted_flag(root, cfg, project):
    _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["a"])],
        [_raw_task_entry("T-01", milestone="M-01")])
    plan.start_task(root, cfg, project, "T-01"); plan.done_task(root, cfg, project, "T-01")
    # The milestone entry carries only its heading + Exit block — no completion field.
    body = markdown.section_body(_ppath(root, cfg, project).read_text(), "## Milestones")
    for banned in ("Complete", "Status:", "Progress:", "Done:"):
        assert banned not in body


def test_milestone_progress_empty_when_no_milestones(root, cfg, project):
    _good_plan(root, cfg, project)  # tasks, but no milestones
    view = plan.milestone_progress(root, cfg, project)
    assert view["milestones"] == [] and view["current"] is None


# --- Task milestone membership (T-03) -----------------------------------------


def test_add_task_with_milestone_writes_single_field(root, cfg, project):
    _started_plan(root, cfg, project)
    plan.add_milestone(root, cfg, project, "First", exit_items=["a"], today="2026-06-22")
    t = plan.add_task(root, cfg, project, "member", acceptance="a", verify="v",
                      implements=["REQ-01"], milestone="M-01", today="2026-06-22")
    assert t.milestone == "M-01"
    text = _ppath(root, cfg, project).read_text()
    assert "- Milestone: M-01" in text
    assert text.count("- Milestone:") == 1  # never two fields on one task


def test_add_task_rejects_unknown_or_absent_milestone(root, cfg, project):
    _started_plan(root, cfg, project)
    with pytest.raises(SpecfloError):  # no milestones exist at all
        plan.add_task(root, cfg, project, "x", acceptance="a", verify="v",
                      implements=["REQ-01"], milestone="M-01")
    plan.add_milestone(root, cfg, project, "First", exit_items=["a"], today="2026-06-22")
    with pytest.raises(SpecfloError):  # M-09 not present
        plan.add_task(root, cfg, project, "x", acceptance="a", verify="v",
                      implements=["REQ-01"], milestone="M-09")


def test_set_milestone_reassigns_in_place_without_duplicating(root, cfg, project):
    _started_plan(root, cfg, project)
    plan.add_milestone(root, cfg, project, "First", exit_items=["a"], today="2026-06-22")
    plan.add_milestone(root, cfg, project, "Second", exit_items=["b"], today="2026-06-22")
    plan.add_task(root, cfg, project, "t", acceptance="a", verify="v",
                  implements=["REQ-01"], milestone="M-01", today="2026-06-22")  # T-01
    t = plan.set_milestone(root, cfg, project, "T-01", "M-02", today="2026-06-22")
    assert t.milestone == "M-02"
    text = _ppath(root, cfg, project).read_text()
    assert "- Milestone: M-02" in text
    assert "- Milestone: M-01" not in text
    assert text.count("- Milestone:") == 1  # reassigned in place, not appended


def test_set_milestone_assigns_a_previously_unassigned_task(root, cfg, project):
    _started_plan(root, cfg, project)
    plan.add_milestone(root, cfg, project, "First", exit_items=["a"], today="2026-06-22")
    plan.add_task(root, cfg, project, "t", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")  # T-01, no milestone
    plan.set_milestone(root, cfg, project, "T-01", "M-01", today="2026-06-22")
    tasks = {t.id: t for t in plan.list_tasks(root, cfg, project)}
    assert tasks["T-01"].milestone == "M-01"


def test_set_milestone_rejects_unknown_task_or_milestone(root, cfg, project):
    _started_plan(root, cfg, project)
    plan.add_milestone(root, cfg, project, "First", exit_items=["a"], today="2026-06-22")
    plan.add_task(root, cfg, project, "t", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")  # T-01
    with pytest.raises(SpecfloError):  # unknown milestone
        plan.set_milestone(root, cfg, project, "T-01", "M-09")
    with pytest.raises(SpecfloError):  # unknown task
        plan.set_milestone(root, cfg, project, "T-99", "M-01")


# --- milestone show detail (T-04) ---------------------------------------------


def test_milestone_detail_assembles_members_reqs_and_completeness(root, cfg, project):
    _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["ships"])],
        [_raw_task_entry("T-01", milestone="M-01", implements="REQ-01"),
         _raw_task_entry("T-02", milestone="M-01", implements="REQ-01, REQ-02")],
        n_reqs=2)
    d = plan.milestone_detail(root, cfg, project, "M-01")
    assert d["id"] == "M-01" and d["title"] == "First"
    assert d["exit_items"] == ["ships"]
    assert [m["id"] for m in d["members"]] == ["T-01", "T-02"]
    assert d["members"][0]["progress"] == "pending"
    assert (d["done"], d["total"]) == (0, 2)
    assert d["reqs"] == ["REQ-01", "REQ-02"]          # union of member Implements
    assert d["complete"] is False
    plan.start_task(root, cfg, project, "T-01"); plan.done_task(root, cfg, project, "T-01")
    plan.start_task(root, cfg, project, "T-02"); plan.done_task(root, cfg, project, "T-02")
    d2 = plan.milestone_detail(root, cfg, project, "M-01")
    assert d2["done"] == 2 and d2["complete"] is True  # complete only once all done


def test_milestone_detail_req_appears_under_both_milestones(root, cfg, project):
    _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["a"]), ("Second", ["b"])],
        [_raw_task_entry("T-01", milestone="M-01", implements="REQ-01"),
         _raw_task_entry("T-02", milestone="M-02", implements="REQ-01")])
    assert plan.milestone_detail(root, cfg, project, "M-01")["reqs"] == ["REQ-01"]
    assert plan.milestone_detail(root, cfg, project, "M-02")["reqs"] == ["REQ-01"]


def test_milestone_detail_unknown_returns_none(root, cfg, project):
    _started_plan(root, cfg, project)
    plan.add_milestone(root, cfg, project, "First", exit_items=["a"], today="2026-06-22")
    assert plan.milestone_detail(root, cfg, project, "M-09") is None


# --- Milestone-aware validate plan (T-05) -------------------------------------


def _valid_milestoned_plan(root, cfg, project):
    """A plan that passes base validation AND the milestone rules: two milestones
    with non-empty Exit checklists, every task assigned, full REQ coverage."""
    return _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["a"]), ("Second", ["b"])],
        [_raw_task_entry("T-01", milestone="M-01", implements="REQ-01"),
         _raw_task_entry("T-02", milestone="M-02", implements="REQ-02", deps=["T-01"])],
        n_reqs=2)


def test_validate_passes_a_fully_assigned_milestoned_plan(root, cfg, project):
    _valid_milestoned_plan(root, cfg, project)
    assert plan.validate_plan(root, cfg, project) == []


def test_validate_flags_task_without_milestone_when_milestones_exist(root, cfg, project):
    # Same plan, but strip T-02's Milestone field -> membership violation (REQ-08).
    path = _valid_milestoned_plan(root, cfg, project)
    path.write_text(path.read_text().replace("- Milestone: M-02\n", ""))
    issues = plan.validate_plan(root, cfg, project)
    assert any("T-02" in i and "milestone" in i.lower() for i in issues)


def test_validate_flags_empty_milestone(root, cfg, project):
    # M-02 has a non-empty Exit but no member task (REQ-09).
    _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["a"]), ("Second", ["b"])],
        [_raw_task_entry("T-01", milestone="M-01", implements="REQ-01"),
         _raw_task_entry("T-02", milestone="M-01", implements="REQ-02")],
        n_reqs=2)
    issues = plan.validate_plan(root, cfg, project)
    assert any("M-02" in i and "member" in i.lower() for i in issues)


def test_validate_flags_milestone_with_empty_exit(root, cfg, project):
    path = _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["placeholder"])],
        [_raw_task_entry("T-01", milestone="M-01", implements="REQ-01")])
    path.write_text(path.read_text().replace("  - placeholder\n", ""))  # empty the Exit block
    issues = plan.validate_plan(root, cfg, project)
    assert any("M-01" in i and "exit" in i.lower() for i in issues)


def test_validate_flags_unknown_milestone_reference(root, cfg, project):
    path = _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["a"])],
        [_raw_task_entry("T-01", milestone="M-01", implements="REQ-01")])
    path.write_text(path.read_text().replace("- Milestone: M-01\n", "- Milestone: M-09\n"))
    issues = plan.validate_plan(root, cfg, project)
    assert any("T-01" in i and "M-09" in i for i in issues)


def test_validate_ignores_milestone_rules_when_no_milestones(root, cfg, project):
    # A milestone-free plan: tasks carry no Milestone field and validate still passes
    # (REQ-04 dormancy) — no membership/empty-milestone/exit issues appear.
    _good_plan(root, cfg, project)
    issues = plan.validate_plan(root, cfg, project)
    assert issues == []
    assert not any("milestone" in i.lower() for i in issues)


# --- validate plan: dependency direction + milestone coverage (T-06) ----------


def test_validate_flags_forward_milestone_dependency(root, cfg, project):
    # T-01 (M-01) depends on T-02 (M-02) — a forward dependency (REQ-11).
    _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["a"]), ("Second", ["b"])],
        [_raw_task_entry("T-01", milestone="M-01", implements="REQ-01", deps=["T-02"]),
         _raw_task_entry("T-02", milestone="M-02", implements="REQ-02")],
        n_reqs=2)
    issues = plan.validate_plan(root, cfg, project)
    assert any("T-01" in i and "T-02" in i and ("forward" in i.lower() or "later" in i.lower())
               for i in issues)


def test_validate_allows_backward_and_same_milestone_dependencies(root, cfg, project):
    # T-02 (M-02) -> T-01 (M-01) is backward; T-03 (M-02) -> T-02 (M-02) is same-milestone.
    _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["a"]), ("Second", ["b"])],
        [_raw_task_entry("T-01", milestone="M-01", implements="REQ-01"),
         _raw_task_entry("T-02", milestone="M-02", implements="REQ-02", deps=["T-01"]),
         _raw_task_entry("T-03", milestone="M-02", implements="REQ-03", deps=["T-02"])],
        n_reqs=3)
    assert plan.validate_plan(root, cfg, project) == []


def test_validate_flags_req_not_covered_by_any_milestone(root, cfg, project):
    # REQ-02 is implemented only by T-02, which belongs to no milestone, so it is
    # covered task-wise but falls outside the union of milestone REQ sets (REQ-12).
    _plan_with_milestones_and_tasks(
        root, cfg, project, [("First", ["a"])],
        [_raw_task_entry("T-01", milestone="M-01", implements="REQ-01"),
         _raw_task_entry("T-02", implements="REQ-02")],
        n_reqs=2)
    issues = plan.validate_plan(root, cfg, project)
    assert any("REQ-02" in i and "milestone" in i.lower() for i in issues)


def test_validate_milestone_union_equals_active_reqs_when_valid(root, cfg, project):
    # A fully valid milestoned plan: union of milestone REQ sets == active REQ set,
    # so no coverage issue is raised (REQ-12, positive case).
    _valid_milestoned_plan(root, cfg, project)  # M-01/REQ-01, M-02/REQ-02
    assert plan.validate_plan(root, cfg, project) == []
