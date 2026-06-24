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
    assert "# Checkpoint — my-thing" in text
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
