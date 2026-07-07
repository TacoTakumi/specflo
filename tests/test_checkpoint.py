from specflo import checkpoint, config, projects, workflow


def _project(tmp_path, name="My Thing"):
    cfg = config.init_config(tmp_path)
    project = projects.create_project(tmp_path, cfg, name)
    return cfg, project


def test_build_checkpoint_brainstorm_phase(tmp_path):
    _cfg, project = _project(tmp_path)
    data = checkpoint.build_checkpoint(tmp_path, project, today="2026-06-21")
    assert data["project"] == "my-thing"
    assert data["phase"] == "brainstorm"
    assert data["generated"] == "2026-06-21"
    assert data["do_next"] == workflow.next_step("brainstorm")
    # only project.md exists so far
    assert data["read_first"] == ["docs/projects/my-thing/project.md"]
    assert data["path"] == "docs/projects/my-thing/checkpoint.md"


def test_build_checkpoint_lists_only_existing_artifacts_in_order(tmp_path):
    _cfg, project = _project(tmp_path)
    (project.path / "spec.md").write_text("y")        # created out of order
    (project.path / "brainstorm.md").write_text("x")
    data = checkpoint.build_checkpoint(tmp_path, project, today="2026-06-21")
    assert data["read_first"] == [
        "docs/projects/my-thing/project.md",
        "docs/projects/my-thing/brainstorm.md",   # pipeline order, not creation order
        "docs/projects/my-thing/spec.md",
    ]


def test_render_checkpoint_has_the_sections(tmp_path):
    _cfg, project = _project(tmp_path)
    data = checkpoint.build_checkpoint(tmp_path, project, today="2026-06-21")
    text = checkpoint.render_checkpoint(data)
    assert "# Checkpoint - my-thing" in text
    assert "generated 2026-06-21" in text
    assert "## Read first" in text
    assert "## Do next" in text
    assert workflow.next_step("brainstorm") in text
    assert "specflo checkpoint" in text


def test_write_checkpoint_writes_and_overwrites(tmp_path):
    _cfg, project = _project(tmp_path)
    path = checkpoint.write_checkpoint(tmp_path, project, today="2026-06-21")
    assert path == project.path / "checkpoint.md"
    first = path.read_text()
    assert "phase: brainstorm" in first
    # re-rendering after a phase change overwrites in place
    project.phase = "spec"
    checkpoint.write_checkpoint(tmp_path, project, today="2026-06-21")
    assert "phase: spec" in path.read_text()
    assert path.read_text() != first


def test_checkpoint_names_next_task_at_plan_phase(tmp_path):
    from specflo import checkpoint, config, plan, projects, spec
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-06-22")
    spec.start_spec(tmp_path, cfg, "thing", today="2026-06-22")
    spec.add_requirement(tmp_path, cfg, "thing", "r", acceptance="a", today="2026-06-22")
    # move to plan phase by hand (advance is tested elsewhere)
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    proj_md.write_text(proj_md.read_text().replace("phase: brainstorm", "phase: plan"))
    plan.start_plan(tmp_path, cfg, "thing", today="2026-06-22")
    plan.add_task(tmp_path, cfg, "thing", "build it", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-22")
    project = projects.load_project(tmp_path, cfg, "thing")
    payload = checkpoint.build_checkpoint(tmp_path, project, today="2026-06-22")
    assert "T-01" in payload["do_next"]


def test_checkpoint_execute_phase_progress_aware(tmp_path):
    from specflo import checkpoint, config, plan, projects, spec
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-06-24")
    spec.start_spec(tmp_path, cfg, "thing", today="2026-06-24")
    spec.add_requirement(tmp_path, cfg, "thing", "r", acceptance="a", today="2026-06-24")
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    proj_md.write_text(proj_md.read_text().replace("phase: brainstorm", "phase: execute"))
    plan.start_plan(tmp_path, cfg, "thing", today="2026-06-24")
    plan.add_task(tmp_path, cfg, "thing", "build it", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-06-24")     # T-01 pending
    project = projects.load_project(tmp_path, cfg, "thing")
    payload = checkpoint.build_checkpoint(tmp_path, project, today="2026-06-24")
    assert "T-01" in payload["do_next"]                          # names the next task
    assert "task show" in payload["do_next"]


# --- shelved-aware checkpoint (T-11) -------------------------------------


def _shelved_project(tmp_path, reason="paused", phase="plan"):
    """A 'My Thing' project moved to ``phase`` then shelved with ``reason``."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "My Thing")
    proj_md = tmp_path / "docs" / "projects" / "my-thing" / "project.md"
    proj_md.write_text(proj_md.read_text().replace("phase: brainstorm", f"phase: {phase}"))
    project = projects.shelve_project(tmp_path, cfg, "my-thing", reason=reason)
    return cfg, project


def test_build_checkpoint_shelved_directs_to_resume_and_preserves_phase(tmp_path):
    _cfg, project = _shelved_project(tmp_path, reason="paused", phase="plan")
    data = checkpoint.build_checkpoint(tmp_path, project, today="2026-06-29")
    assert data["phase"] == "plan"               # recorded phase preserved for resume
    assert data["status"] == "shelved"
    assert data["shelved_reason"] == "paused"
    assert data["do_next"] == workflow.next_step("plan", shelved=True)
    assert "resume" in data["do_next"].lower()   # directs to resume, not the work step
    assert data["do_next"] != workflow.next_step("plan")


def test_render_checkpoint_shelved_identifies_and_shows_reason(tmp_path):
    _cfg, project = _shelved_project(tmp_path, reason="waiting on api", phase="spec")
    text = checkpoint.render_checkpoint(
        checkpoint.build_checkpoint(tmp_path, project, today="2026-06-29")
    )
    assert "shelved" in text.lower()    # identified as shelved
    assert "waiting on api" in text     # reason shown when set
    assert "resume" in text.lower()     # Do next directs to resume
    assert "phase: spec" in text        # recorded phase preserved


def test_render_checkpoint_shelved_without_reason_still_identifies(tmp_path):
    _cfg, project = _shelved_project(tmp_path, reason=None, phase="plan")
    text = checkpoint.render_checkpoint(
        checkpoint.build_checkpoint(tmp_path, project, today="2026-06-29")
    )
    assert "shelved" in text.lower()


# --- current-milestone-aware checkpoint (T-08) ---------------------------


def _plan_at_execute(tmp_path, with_milestone=False):
    """A 'Thing' project at execute with T-01 pending, optionally in M-01."""
    from specflo import config, plan, projects, spec
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-07-02")
    spec.start_spec(tmp_path, cfg, "thing", today="2026-07-02")
    spec.add_requirement(tmp_path, cfg, "thing", "r", acceptance="a", today="2026-07-02")
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    proj_md.write_text(proj_md.read_text().replace("phase: brainstorm", "phase: execute"))
    plan.start_plan(tmp_path, cfg, "thing", today="2026-07-02")
    plan.add_task(tmp_path, cfg, "thing", "build it", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-07-02")     # T-01 pending
    if with_milestone:
        plan.add_milestone(tmp_path, cfg, "thing", "First", exit_items=["ships"],
                           today="2026-07-02")                   # M-01
        plan.set_milestone(tmp_path, cfg, "thing", "T-01", "M-01", today="2026-07-02")
    return cfg, projects.load_project(tmp_path, cfg, "thing")


def test_checkpoint_names_current_milestone_when_milestones_exist(tmp_path):
    _cfg, project = _plan_at_execute(tmp_path, with_milestone=True)
    payload = checkpoint.build_checkpoint(tmp_path, project, today="2026-07-02")
    assert payload["milestone"]["id"] == "M-01"
    assert payload["milestone"]["done"] == 0 and payload["milestone"]["total"] == 1
    text = checkpoint.render_checkpoint(payload)
    assert "M-01" in text and "First" in text           # named in the resume block


def test_checkpoint_omits_milestone_line_for_a_milestone_free_plan(tmp_path):
    _cfg, project = _plan_at_execute(tmp_path, with_milestone=False)
    payload = checkpoint.build_checkpoint(tmp_path, project, today="2026-07-02")
    assert payload["milestone"] is None
    assert "milestone" not in checkpoint.render_checkpoint(payload).lower()


# --- soft milestone-boundary verify beat (T-09) --------------------------


def _plan_at_boundary(tmp_path):
    """A 'Thing' at execute with M-01 (T-01) done and M-02 (T-02) pending — i.e.
    sitting exactly at the M-01 -> M-02 milestone boundary."""
    from specflo import config, plan, projects, spec
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-07-02")
    spec.start_spec(tmp_path, cfg, "thing", today="2026-07-02")
    spec.add_requirement(tmp_path, cfg, "thing", "r", acceptance="a", today="2026-07-02")
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    proj_md.write_text(proj_md.read_text().replace("phase: brainstorm", "phase: execute"))
    plan.start_plan(tmp_path, cfg, "thing", today="2026-07-02")
    plan.add_milestone(tmp_path, cfg, "thing", "First", exit_items=["ships to prod"],
                       today="2026-07-02")                                   # M-01
    plan.add_milestone(tmp_path, cfg, "thing", "Second", exit_items=["b"],
                       today="2026-07-02")                                   # M-02
    plan.add_task(tmp_path, cfg, "thing", "build it", acceptance="a", verify="v",
                  implements=["REQ-01"], milestone="M-01", today="2026-07-02")   # T-01
    plan.add_task(tmp_path, cfg, "thing", "more", acceptance="a", verify="v",
                  implements=["REQ-01"], milestone="M-02", today="2026-07-02")   # T-02
    plan.start_task(tmp_path, cfg, "thing", "T-01", today="2026-07-02")
    plan.done_task(tmp_path, cfg, "thing", "T-01", today="2026-07-02")
    return cfg, projects.load_project(tmp_path, cfg, "thing")


def test_checkpoint_surfaces_boundary_beat_at_a_milestone_boundary(tmp_path):
    _cfg, project = _plan_at_boundary(tmp_path)
    payload = checkpoint.build_checkpoint(tmp_path, project, today="2026-07-02")
    assert payload["boundary"]["id"] == "M-01"
    text = checkpoint.render_checkpoint(payload)
    assert "ships to prod" in text          # the just-completed milestone's Exit checklist
    assert "proceed" in text.lower()        # user-gated proceed prompt


def test_checkpoint_has_no_boundary_beat_for_a_milestone_free_plan(tmp_path):
    _cfg, project = _plan_at_execute(tmp_path, with_milestone=False)
    payload = checkpoint.build_checkpoint(tmp_path, project, today="2026-07-02")
    assert payload["boundary"] is None
    assert "exit checklist" not in checkpoint.render_checkpoint(payload).lower()


def test_milestone_free_checkpoint_carries_no_milestone_vocabulary(tmp_path):
    # REQ-04 consolidated guard: a zero-milestone checkpoint is pre-feature — both
    # payload fields dormant and none of the milestone vocabulary in the render.
    _cfg, project = _plan_at_execute(tmp_path, with_milestone=False)
    payload = checkpoint.build_checkpoint(tmp_path, project, today="2026-07-02")
    assert payload["milestone"] is None and payload["boundary"] is None
    text = checkpoint.render_checkpoint(payload).lower()
    for word in ("milestone", "exit checklist", "proceed"):
        assert word not in text, f"checkpoint leaked {word!r}: {text}"


# --- derived read-path doneness for brainstorm/spec/plan (T-03) -----------


def _validating_spec_project(tmp_path):
    """A spec-phase 'Thing' whose spec.md passes validate_spec.

    Returns ``(cfg, project, spec_md_path)``.
    """
    from specflo import spec
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-07-06")
    spec.start_spec(tmp_path, cfg, "thing", today="2026-07-06")
    spec.add_requirement(tmp_path, cfg, "thing", "a req", acceptance="it passes",
                         today="2026-07-06")
    spec_md = tmp_path / "docs" / "projects" / "thing" / "spec.md"
    spec_md.write_text(
        spec_md.read_text()
        .replace("### In scope\n<!-- required, non-empty -->",
                 "### In scope\n- the CLI.")
        .replace("### Out of scope\n"
                 "<!-- required, non-empty; carried from the brainstorm's "
                 "Out of scope / Deferred -->",
                 "### Out of scope\n- the GUI.")
    )
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    proj_md.write_text(proj_md.read_text().replace("phase: brainstorm", "phase: spec"))
    return cfg, projects.load_project(tmp_path, cfg, "thing"), spec_md


def test_checkpoint_spec_that_validates_offers_advance(tmp_path):
    from specflo import spec
    cfg, project, _spec_md = _validating_spec_project(tmp_path)
    assert spec.validate_spec(tmp_path, cfg, "thing") == []      # precondition
    payload = checkpoint.build_checkpoint(tmp_path, project, cfg=cfg, today="2026-07-06")
    assert "specflo advance" in payload["do_next"]               # offers the move
    assert "plan" in payload["do_next"]                          # names the next phase
    # a bare build (no cfg) does not derive — keeps the static work hint
    bare = checkpoint.build_checkpoint(tmp_path, project, today="2026-07-06")
    assert bare["do_next"] == workflow.next_step("spec")


def test_checkpoint_derives_doneness_on_every_read(tmp_path):
    # REQ-02/03: recomputed from the artifact each read; breaking validation flips
    # the very next build back to the work hint with no intervening command.
    from specflo import spec
    cfg, project, spec_md = _validating_spec_project(tmp_path)
    first = checkpoint.build_checkpoint(tmp_path, project, cfg=cfg, today="2026-07-06")
    assert "specflo advance" in first["do_next"]
    spec_md.write_text(spec_md.read_text().replace("### In scope\n- the CLI.",
                                                   "### In scope\n"))
    assert spec.validate_spec(tmp_path, cfg, "thing")            # now fails
    second = checkpoint.build_checkpoint(tmp_path, project, cfg=cfg, today="2026-07-06")
    assert second["do_next"] == workflow.next_step("spec")       # reverted, no command
    assert "specflo advance" not in second["do_next"]


def test_build_checkpoint_mutates_no_project_state(tmp_path):
    # REQ-04: deriving doneness never changes the phase or status.
    cfg, project, _spec_md = _validating_spec_project(tmp_path)
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    before, phase, status = proj_md.read_text(), project.phase, project.status
    checkpoint.build_checkpoint(tmp_path, project, cfg=cfg, today="2026-07-06")
    assert proj_md.read_text() == before
    assert (project.phase, project.status) == (phase, status)


def test_checkpoint_execute_phase_unchanged_when_cfg_passed(tmp_path):
    # REQ-05: execute keeps its progress-based hint; cfg never fires the validator.
    cfg, project = _plan_at_execute(tmp_path, with_milestone=False)
    with_cfg = checkpoint.build_checkpoint(tmp_path, project, cfg=cfg, today="2026-07-02")
    without = checkpoint.build_checkpoint(tmp_path, project, today="2026-07-02")
    assert with_cfg["do_next"] == without["do_next"]
    assert "T-01" in with_cfg["do_next"]


def test_checkpoint_shelved_unchanged_when_cfg_passed(tmp_path):
    cfg, project = _shelved_project(tmp_path, reason="paused", phase="spec")
    payload = checkpoint.build_checkpoint(tmp_path, project, cfg=cfg, today="2026-06-29")
    assert payload["do_next"] == workflow.next_step("spec", shelved=True)
