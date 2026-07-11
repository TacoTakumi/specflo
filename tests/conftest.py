"""Shared pytest fixtures for the specflo test suite."""

import pytest


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
