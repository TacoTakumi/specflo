"""An open task that depends on a deferred task is told the way out.

A deferred task is frozen, so it never becomes done. A task that still
depends on it can not start. Every place that reports this names the
follow-up the work went to and the command that drops the dependency.
"""

import pytest
from typer.testing import CliRunner

from specflo import config, followup, plan, projects, spec
from specflo.cli import app

runner = CliRunner()


@pytest.fixture
def stuck(tmp_path, monkeypatch):
    """A finished task that depended on a task deferred after it, reopened."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    spec.start_spec(tmp_path, cfg, "thing")
    spec.add_requirement(tmp_path, cfg, "thing", "Prints help", acceptance="exits 0")
    plan.start_plan(tmp_path, cfg, "thing")
    first = plan.add_task(tmp_path, cfg, "thing", "Build the parser", "a", "uv run pytest",
                          implements=["REQ-01"]).id
    second = plan.add_task(tmp_path, cfg, "thing", "Build the printer", "a", "uv run pytest",
                           implements=["REQ-01"]).id
    third = plan.add_task(tmp_path, cfg, "thing", "Use the printer", "a", "uv run pytest",
                          implements=["REQ-01"], depends_on=[second]).id
    projects.switch_project(tmp_path, cfg, "thing")
    record = projects.project_dir(tmp_path, cfg, "thing") / projects.PROJECT_FILENAME
    record.write_text(record.read_text().replace("phase: brainstorm", "phase: execute"))
    monkeypatch.chdir(tmp_path)
    for task_id in (first, third):
        plan.start_task(tmp_path, cfg, "thing", task_id, force=True)
        plan.done_task(tmp_path, cfg, "thing", task_id)
    _, entry = followup.defer_task(tmp_path, cfg, "thing", second, "Build it later", "No time")
    plan.reopen_task(tmp_path, cfg, "thing", third)
    remedy = f"specflo task edit {third} --drop-depends-on {second}"
    return tmp_path, cfg, second, third, entry.id, remedy


def test_validate_names_the_followup_and_the_way_out(stuck):
    root, cfg, second, third, followup_id, remedy = stuck

    result = runner.invoke(app, ["validate", "plan"])

    assert result.exit_code != 0
    assert f"deferred to {followup_id}" in result.output
    assert remedy in result.output


def test_task_start_names_the_followup_and_the_way_out(stuck):
    root, cfg, second, third, followup_id, remedy = stuck

    result = runner.invoke(app, ["task", "start", third])

    assert result.exit_code != 0
    assert f"deferred to {followup_id}" in result.output
    assert remedy in result.output
    assert "Finish it first" not in result.output


def test_status_names_the_followup_and_the_way_out(stuck):
    root, cfg, second, third, followup_id, remedy = stuck

    result = runner.invoke(app, ["status"])

    assert f"deferred to {followup_id}" in result.output
    assert remedy in result.output


def test_the_checkpoint_names_the_followup_and_the_way_out(stuck):
    root, cfg, second, third, followup_id, remedy = stuck

    result = runner.invoke(app, ["checkpoint"])

    assert f"deferred to {followup_id}" in result.output
    assert remedy in result.output


def test_task_show_names_the_followup_and_the_way_out(stuck):
    root, cfg, second, third, followup_id, remedy = stuck

    result = runner.invoke(app, ["task", "show"])

    assert result.exit_code != 0
    assert f"deferred to {followup_id}" in result.output
    assert remedy in result.output


def test_the_way_out_works(stuck):
    root, cfg, second, third, followup_id, remedy = stuck

    plan.edit_task(root, cfg, "thing", third, drop_depends_on=[second])

    assert plan.validate_plan(root, cfg, "thing") == []
    assert plan.current_task_id(root, cfg, "thing") == third


def test_a_dependency_that_is_only_not_done_keeps_its_message(stuck):
    root, cfg, second, third, followup_id, remedy = stuck
    fourth = plan.add_task(root, cfg, "thing", "Ship it", "a", "uv run pytest",
                           implements=["REQ-01"], depends_on=[third]).id

    result = runner.invoke(app, ["task", "start", fourth])

    assert result.exit_code != 0
    assert f"{fourth} depends on {third}, not done yet. Finish it first" in result.output
    assert "deferred" not in result.output


def test_a_superseded_dependency_keeps_its_rewire_hint(tmp_path, monkeypatch):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    spec.start_spec(tmp_path, cfg, "thing")
    spec.add_requirement(tmp_path, cfg, "thing", "Prints help", acceptance="exits 0")
    plan.start_plan(tmp_path, cfg, "thing")
    first = plan.add_task(tmp_path, cfg, "thing", "Build the parser", "a", "uv run pytest",
                          implements=["REQ-01"]).id
    second = plan.add_task(tmp_path, cfg, "thing", "Use the parser", "a", "uv run pytest",
                           implements=["REQ-01"], depends_on=[first]).id
    newer = plan.add_task(tmp_path, cfg, "thing", "Build the parser again", "a",
                          "uv run pytest", implements=["REQ-01"], supersedes=first).id

    blocks = plan.blocked_on_superseded_from_doc(
        plan.plan_path(tmp_path, cfg, "thing").read_text()
    )

    assert plan.superseded_block_remediation(blocks) == [
        f"{second} depends on superseded {first} (superseded by {newer}); "
        f"run: specflo task rewire --from {first} --to {newer}"
    ]


# --- a dependency that was superseded, and whose replacement is deferred ------


def _superseded_then_deferred(tmp_path, monkeypatch, chain=1):
    """A pending task that depends on a task superseded ``chain`` times, with
    the last replacement deferred by a field written into the plan text: the
    defer verb refuses to make this state, but a plan can hold it."""
    from specflo import markdown

    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    spec.start_spec(tmp_path, cfg, "thing")
    spec.add_requirement(tmp_path, cfg, "thing", "Prints help", acceptance="exits 0")
    plan.start_plan(tmp_path, cfg, "thing")
    old = plan.add_task(tmp_path, cfg, "thing", "Build the parser", "a", "uv run pytest",
                        implements=["REQ-01"]).id
    waiting = plan.add_task(tmp_path, cfg, "thing", "Use the parser", "a", "uv run pytest",
                            implements=["REQ-01"], depends_on=[old]).id
    last = old
    for n in range(chain):
        last = plan.add_task(tmp_path, cfg, "thing", f"Build the parser, try {n + 2}", "a",
                             "uv run pytest", implements=["REQ-01"], supersedes=last).id
    path = plan.plan_path(tmp_path, cfg, "thing")
    path.write_text(markdown.set_entry_field(path.read_text(), last, "Deferred to", "FU-07"))
    projects.switch_project(tmp_path, cfg, "thing")
    record = projects.project_dir(tmp_path, cfg, "thing") / projects.PROJECT_FILENAME
    record.write_text(record.read_text().replace("phase: brainstorm", "phase: execute"))
    monkeypatch.chdir(tmp_path)
    return old, waiting, last, f"specflo task edit {waiting} --drop-depends-on {old}"


@pytest.fixture(params=[1, 2], ids=["one-supersession", "two-supersessions"])
def replaced(request, tmp_path, monkeypatch):
    return _superseded_then_deferred(tmp_path, monkeypatch, chain=request.param)


@pytest.mark.parametrize("command", [
    ["validate", "plan"], ["status"], ["checkpoint"], ["task", "show"],
], ids=["validate", "status", "checkpoint", "task-show"])
def test_a_deferred_replacement_names_the_followup_and_the_way_out(replaced, command):
    old, waiting, last, remedy = replaced

    result = runner.invoke(app, command)

    assert "deferred to FU-07" in result.output
    assert remedy in result.output
    assert "task rewire" not in result.output


def test_task_start_on_a_deferred_replacement_names_the_way_out(replaced):
    old, waiting, last, remedy = replaced

    result = runner.invoke(app, ["task", "start", waiting])

    assert result.exit_code != 0
    assert "deferred to FU-07" in result.output
    assert remedy in result.output
    assert "Finish it first" not in result.output
