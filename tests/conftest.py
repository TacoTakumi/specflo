"""Shared pytest fixtures and helpers for the specflo test suite."""

import ast
import inspect
import textwrap

import pytest


def executable_identifiers(obj) -> str:
    """Every identifier and string *value* in ``obj``, docstrings excluded.

    Source-scan tests assert that a module or function does not *do* something —
    read auto-run state, compose its own continuation wording, name a harness
    trigger. A raw substring scan over the source cannot express that: it also
    flags comments and docstrings describing the very thing being ruled out, so
    documenting an invariant would break its own test.

    This walks the AST instead. Comments never reach it, statement-position
    docstrings are dropped, and what remains is what the code actually evaluates:
    names, attributes, arguments, imports, and string literals (f-string parts
    included). Accepts a module or a function.
    """
    # dedent, never cleandoc: cleandoc strips the common indent of lines 2+ while
    # leaving line 1 alone, which de-indents a function's body relative to its own
    # `def` and raises IndentationError. A decorated function happens to survive
    # that (its line 2 is the `def` at column 0), which is exactly the kind of
    # accident this helper should not rely on.
    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
    scopes = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, scopes) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body.pop(0)

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            found.append(node.attr)
        elif isinstance(node, ast.arg):
            found.append(node.arg)
        elif isinstance(node, ast.alias):
            found.append(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
    return "\n".join(found).lower()


@pytest.fixture
def fixture_roots(tmp_path, monkeypatch):
    """Redirect agentsquire's harness roots to throwaway fixture dirs (D-12).

    agentsquire reads AGENTSQUIRE_HOME / AGENTSQUIRE_PROJECT to locate the
    user-scope and project-scope roots; each carries a `.claude/` marker so the
    claude-code harness is detected. This keeps `specflo skills` verbs off the
    real ~/.claude during tests -- the live-edit dev symlinks are never touched.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".claude").mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    monkeypatch.setenv("AGENTSQUIRE_HOME", str(home))
    monkeypatch.setenv("AGENTSQUIRE_PROJECT", str(project))
    return home, project
