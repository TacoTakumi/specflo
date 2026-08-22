"""Guards for the shared phase->validator module (REQ-12).

The phase->validator map lives in a neutral ``specflo.validators`` so that
``checkpoint``, ``status`` and ``cli`` can share it without the ``cli`` import
cycle. These tests pin that: the map is defined once, ``cli`` references it, the
four phase modules import cleanly together, and the read-path modules never
reach for ``cli``.
"""

import ast
from pathlib import Path

from specflo import brainstorm, checkpoint, cli, plan, spec, status, validators


def test_validators_maps_each_phase_to_its_real_validator():
    assert validators.VALIDATORS == {
        "brainstorm": brainstorm.validate_brainstorm,
        "spec": spec.validate_spec,
        "plan": plan.validate_plan,
        "execute": validators.execute_issues,
    }


def test_cli_references_the_shared_map_rather_than_redefining_it():
    # Same object identity -> the map is defined once, in validators.
    assert cli.VALIDATORS is validators.VALIDATORS


def test_the_four_phase_modules_import_together_without_a_cycle():
    # Importing all four at module top would already raise on a cycle; assert
    # they resolved to real modules so this stays a meaningful guard.
    for module in (validators, checkpoint, status, cli):
        assert module.__name__.startswith("specflo.")


def _imported_modules(source_path: Path) -> set[str]:
    """The dotted module names a source file imports (best-effort, static)."""
    tree = ast.parse(source_path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # `from . import cli` -> module is None, names include "cli";
            # `from .cli import x` -> module is "cli".
            if node.module:
                names.add(node.module)
            names.update(alias.name for alias in node.names)
    return names


def test_read_path_modules_never_import_cli():
    pkg = Path(checkpoint.__file__).parent
    for name in ("checkpoint", "status"):
        imported = _imported_modules(pkg / f"{name}.py")
        assert "cli" not in imported, f"{name}.py must not import cli (REQ-12)"
        assert not any(
            i.endswith(".cli") or i == "specflo.cli" for i in imported
        ), f"{name}.py must not import cli (REQ-12)"


# --- the review gate on execute -> complete (review-rounds REQ-21, REQ-16) ----
# The execute validator is what `specflo advance` runs to decide whether the
# project may complete, so the review gate lands there and nowhere else.


def _execute_ready(tmp_path):
    """A 'Thing' at execute whose only task is done - reconcile is clean."""
    from specflo import config, projects, spec as spec_module
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing", created="2026-08-01")
    projects.switch_project(tmp_path, cfg, "Thing")
    spec_module.start_spec(tmp_path, cfg, "thing", today="2026-08-01")
    spec_module.add_requirement(tmp_path, cfg, "thing", "r", acceptance="a",
                                today="2026-08-01")
    proj_md = tmp_path / "docs" / "projects" / "thing" / "project.md"
    proj_md.write_text(proj_md.read_text().replace("phase: brainstorm", "phase: execute"))
    plan.start_plan(tmp_path, cfg, "thing", today="2026-08-01")
    plan.add_task(tmp_path, cfg, "thing", "build it", acceptance="a", verify="v",
                  implements=["REQ-01"], today="2026-08-01")
    plan.start_task(tmp_path, cfg, "thing", "T-01", today="2026-08-01")
    plan.done_task(tmp_path, cfg, "thing", "T-01", today="2026-08-01")
    assert plan.reconcile_issues(tmp_path, cfg, "thing") == []
    return cfg


def _close(tmp_path, cfg, verdict, reason=None):
    from specflo import review
    review.start_round(tmp_path, cfg, "thing", today="2026-08-01")
    review.close_round(tmp_path, cfg, "thing", verdict, reason=reason,
                       today="2026-08-02")


def test_execute_review_gate_blocks_when_no_round_exists(tmp_path):
    cfg = _execute_ready(tmp_path)
    issues = validators.execute_issues(tmp_path, cfg, "thing")
    assert len(issues) == 1
    assert "review" in issues[0].lower()


def test_execute_review_gate_blocks_while_the_latest_round_is_open(tmp_path):
    # REQ-19: an earlier pass is not current once a newer round opens.
    from specflo import review
    cfg = _execute_ready(tmp_path)
    _close(tmp_path, cfg, "ready-to-merge")
    review.start_round(tmp_path, cfg, "thing", today="2026-08-03")
    issues = validators.execute_issues(tmp_path, cfg, "thing")
    assert len(issues) == 1
    assert "review-2.md" in issues[0]


def test_execute_review_gate_blocks_on_changes_requested(tmp_path):
    cfg = _execute_ready(tmp_path)
    _close(tmp_path, cfg, "changes-requested")
    issues = validators.execute_issues(tmp_path, cfg, "thing")
    assert len(issues) == 1
    assert "review-1.md" in issues[0]


def test_execute_review_gate_passes_on_ready_to_merge_and_waived(tmp_path):
    for verdict, reason in (("ready-to-merge", None), ("waived", "not reviewing")):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _execute_ready(root)
            _close(root, cfg, verdict, reason=reason)
            assert validators.execute_issues(root, cfg, "thing") == [], verdict


def test_execute_review_gate_ignores_the_rounds_findings(tmp_path):
    # REQ-16: the gate keys on the verdict alone; a passing round may list nits.
    cfg = _execute_ready(tmp_path)
    _close(tmp_path, cfg, "ready-to-merge")
    round_file = tmp_path / "docs" / "projects" / "thing" / "review-1.md"
    round_file.write_text(
        round_file.read_text().replace("## Findings\n", "## Findings\n\n- a nit.\n- another.\n")
    )
    assert validators.execute_issues(tmp_path, cfg, "thing") == []


def test_execute_review_gate_reports_only_after_the_plan_reconciles(tmp_path):
    # An unfinished plan is the first thing to fix; the review issue does not
    # crowd out the task list.
    cfg = _execute_ready(tmp_path)
    plan.reopen_task(tmp_path, cfg, "thing", "T-01", today="2026-08-01")
    issues = validators.execute_issues(tmp_path, cfg, "thing")
    assert any("T-01" in issue for issue in issues)
