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
        "execute": plan.reconcile_issues,
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
