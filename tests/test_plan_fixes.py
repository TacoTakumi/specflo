"""Fix tasks: `task add --fixes F-NN` records the review findings a task fixes.

A task names what it is for with ``--from REQ-NN`` (the requirements it
implements), with ``--fixes F-NN`` (the findings it fixes), or with both.
The findings go in the task's ``- Fixes:`` field; ``task show`` and ``task
list`` print them. A task with neither is refused, and the refusal changes
no file. A hosted add writes the same plan.md as a local one.
"""

import json
import re

import pytest
from typer.testing import CliRunner

from specflo import config, plan, projects, spec
from specflo.cli import app
from specflo.errors import SpecfloError
from test_hosted_parity import _hosted_steps, _local_steps, _pipeline

runner = CliRunner()


def _entry(document: str, entry_id: str) -> list[str]:
    """The field lines of one ``### <id> ...`` entry, up to the next heading."""
    lines = document.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"### {entry_id} "))
    body = []
    for line in lines[start + 1:]:
        if line.startswith("#") or not line.strip():
            break
        body.append(line)
    return body


# --- the module function ------------------------------------------------------


@pytest.fixture
def planned(tmp_path):
    """A project with one requirement and a started plan: (root, cfg, slug)."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    spec.start_spec(tmp_path, cfg, "thing")
    spec.add_requirement(tmp_path, cfg, "thing", "Prints help", acceptance="exits 0")
    plan.start_plan(tmp_path, cfg, "thing")
    return tmp_path, cfg, "thing"


def test_a_task_that_fixes_a_finding_needs_no_requirement(planned):
    root, cfg, slug = planned
    task = plan.add_task(root, cfg, slug, "Fix the close", "the sha is kept", "uv run pytest",
                         implements=[], fixes=["F-02"])
    assert task.fixes == ["F-02"] and task.implements == []
    document = plan.plan_path(root, cfg, slug).read_text()
    assert _entry(document, task.id) == [
        "- Acceptance: the sha is kept",
        "- Verify: uv run pytest",
        "- Fixes: F-02",
        "- Progress: pending",
        "- Status: active",
    ]
    (parsed,) = plan.list_tasks(root, cfg, slug)
    assert parsed.fixes == ["F-02"] and parsed.implements == []


def test_fixes_is_repeatable_and_is_written_beside_the_requirements(planned):
    root, cfg, slug = planned
    task = plan.add_task(root, cfg, slug, "Fix both", "both hold", "uv run pytest",
                         implements=["REQ-01"], fixes=["F-01", "F-02"])
    document = plan.plan_path(root, cfg, slug).read_text()
    assert _entry(document, task.id)[:4] == [
        "- Acceptance: both hold",
        "- Verify: uv run pytest",
        "- Implements: REQ-01",
        "- Fixes: F-01, F-02",
    ]
    (parsed,) = plan.list_tasks(root, cfg, slug)
    assert parsed.fixes == ["F-01", "F-02"] and parsed.implements == ["REQ-01"]


def test_a_task_with_no_fixes_is_written_as_before(planned):
    root, cfg, slug = planned
    task = plan.add_task(root, cfg, slug, "Build help", "help prints", "uv run pytest",
                         implements=["REQ-01"])
    document = plan.plan_path(root, cfg, slug).read_text()
    assert not any(line.startswith("- Fixes:") for line in _entry(document, task.id))
    (parsed,) = plan.list_tasks(root, cfg, slug)
    assert parsed.fixes == []


@pytest.mark.parametrize("fixes", [[], None])
def test_a_task_that_neither_implements_nor_fixes_is_refused(planned, fixes):
    root, cfg, slug = planned
    path = plan.plan_path(root, cfg, slug)
    before = path.read_bytes()
    with pytest.raises(SpecfloError, match=r"--from REQ-NN.*--fixes F-NN"):
        plan.add_task(root, cfg, slug, "Orphan", "a", "v", implements=[], fixes=fixes)
    assert path.read_bytes() == before


def test_a_fix_that_is_not_one_line_is_refused(planned):
    root, cfg, slug = planned
    path = plan.plan_path(root, cfg, slug)
    before = path.read_bytes()
    with pytest.raises(SpecfloError, match="one line"):
        plan.add_task(root, cfg, slug, "Inject", "a", "v", implements=[],
                      fixes=["F-02\n- Progress: done"])
    assert path.read_bytes() == before


def test_the_brief_carries_the_fixes(planned):
    root, cfg, slug = planned
    fix = plan.add_task(root, cfg, slug, "Fix the close", "kept", "uv run pytest",
                        implements=[], fixes=["F-02"])
    both = plan.add_task(root, cfg, slug, "Fix and build", "done", "uv run pytest",
                         implements=["REQ-01"], fixes=["F-01", "F-02"])
    plain = plan.add_task(root, cfg, slug, "Build help", "help prints", "uv run pytest",
                          implements=["REQ-01"])
    brief = plan.task_brief(root, cfg, slug, fix.id)
    assert brief["task"]["fixes"] == ["F-02"]
    lines = plan.render_task_brief(brief).splitlines()
    assert "  Fixes:      F-02" in lines
    assert not any(line.startswith("  Implements:") for line in lines)
    lines = plan.render_task_brief(plan.task_brief(root, cfg, slug, both.id)).splitlines()
    assert "  Implements: REQ-01" in lines and "  Fixes:      F-01, F-02" in lines
    lines = plan.render_task_brief(plan.task_brief(root, cfg, slug, plain.id)).splitlines()
    assert "  Implements: REQ-01" in lines
    assert not any(line.startswith("  Fixes:") for line in lines)


# --- the CLI, local and hosted ------------------------------------------------


def _steps():
    """A plan with T-01, a closed round that asked for changes (blocker F-01,
    should-fix F-02), then the fix tasks and every view of them."""
    pipeline = _pipeline()
    last = next(i for i, (args, _) in enumerate(pipeline) if args[:2] == ["task", "add"])
    fix = ["task", "add", "--acceptance", "the sha is kept", "--verify", "uv run pytest"]
    return [
        *pipeline[: last + 1],
        (["review", "start"], None),
        (["review", "finding", "add", "--severity", "blocker", "--text", "The close drops the sha"], None),
        (["review", "finding", "add", "--severity", "should-fix", "--text", "A message names the wrong command"], None),
        (["review", "done"], None),
        ([*fix, "--text", "Keep the sha", "--fixes", "F-02"], None),
        ([*fix, "--text", "Fix the close", "--fixes", "F-01", "--fixes", "F-02",
          "--from", lambda ids: ids["requirement"]], None),
        (["task", "show", "T-02"], None),
        (["task", "show", "T-03"], None),
        (["task", "show", "T-02", "--json"], None),
        (["task", "list"], None),
        (["task", "list", "--json"], None),
    ]


def _outputs(results) -> dict[tuple, tuple[int, str]]:
    """Each step's ``(exit code, stdout)``, by its arguments."""
    return {tuple(args): (code, text) for args, code, text in results[1:]}


def _add_with_neither():
    """`task add` naming no requirement and no finding, in the current checkout."""
    return runner.invoke(app, ["task", "add", "--text", "Orphan", "--acceptance", "a",
                               "--verify", "v"])


def _without_actor(document: str) -> str:
    return re.sub(r"^- Actor: .*\n", "", document, flags=re.MULTILINE)


def test_task_add_fixes_is_written_and_shown(tmp_path, monkeypatch):
    results, project_dir = _local_steps(tmp_path, monkeypatch, _steps())
    out = _outputs(results)
    assert all(code == 0 for code, _ in out.values()), out
    added = [text for args, (_, text) in out.items() if "--fixes" in args]
    assert added == ["Recorded T-02 (fixes F-02).\n",
                     "Recorded T-03 (implements REQ-01; fixes F-01, F-02).\n"]

    document = (project_dir / "plan.md").read_text()
    assert "- Fixes: F-02" in _entry(document, "T-02")
    assert not any(line.startswith("- Implements:") for line in _entry(document, "T-02"))
    assert _entry(document, "T-03")[2:4] == ["- Implements: REQ-01", "- Fixes: F-01, F-02"]

    shown = out[("task", "show", "T-02")][1].splitlines()
    assert "  Fixes:      F-02" in shown
    shown = out[("task", "show", "T-03")][1].splitlines()
    assert "  Implements: REQ-01" in shown and "  Fixes:      F-01, F-02" in shown
    assert json.loads(out[("task", "show", "T-02", "--json")][1])["task"]["fixes"] == ["F-02"]

    listed = out[("task", "list")][1].splitlines()
    assert "T-01  [pending]  Build help" in next(line for line in listed if "T-01" in line)
    assert "fixes" not in next(line for line in listed if "T-01" in line)
    assert next(line for line in listed if "T-02" in line).endswith("Keep the sha  fixes: F-02")
    assert next(line for line in listed if "T-03" in line).endswith("fixes: F-01, F-02")
    tasks = json.loads(out[("task", "list", "--json")][1])["tasks"]
    assert [t["fixes"] for t in tasks] == [[], ["F-02"], ["F-01", "F-02"]]

    before = (project_dir / "plan.md").read_bytes()
    refused = _add_with_neither()
    assert refused.exit_code == 1, refused.output
    assert "--fixes F-NN" in refused.output
    assert (project_dir / "plan.md").read_bytes() == before


def test_task_add_fixes_on_a_hosted_project_gives_the_same_plan(tmp_path, monkeypatch, live_daemon):
    steps = _steps()
    local, local_dir = _local_steps(tmp_path, monkeypatch, steps)
    local_refusal = _add_with_neither()
    hosted, hosted_dir = _hosted_steps(tmp_path, monkeypatch, live_daemon, steps)
    before = (hosted_dir / "plan.md").read_bytes()
    hosted_refusal = _add_with_neither()

    assert len(local) == len(hosted) == len(steps) + 1
    for mine, theirs in zip(local[1:], hosted[1:]):
        args = mine[0]
        assert mine[1:] == theirs[1:], f"{' '.join(args)}:\nlocal:\n{mine[2]}\nhosted:\n{theirs[2]}"
    hosted_plan = (hosted_dir / "plan.md").read_text()
    assert "- Fixes: F-02" in _entry(hosted_plan, "T-02")
    assert _without_actor(hosted_plan) == _without_actor((local_dir / "plan.md").read_text())

    assert hosted_refusal.exit_code == local_refusal.exit_code == 1
    assert hosted_refusal.output == local_refusal.output
    assert (hosted_dir / "plan.md").read_bytes() == before
