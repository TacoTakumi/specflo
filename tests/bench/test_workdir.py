"""The sealed workdir: a fresh fixture copy outside the repo, one commit, a seeded specflo project."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from modelbench import workdir as wd

LEVELS = ("quick", "fast", "full")


def _git(path: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    out = subprocess.run(
        ["git", "-C", str(path), *args], env=env, capture_output=True, text=True, check=True
    )
    return out.stdout


def _specflo(path: Path, *args: str) -> str:
    out = subprocess.run(
        [wd.find_specflo(), "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return out.stdout


@pytest.mark.parametrize("level", LEVELS)
def test_sealed_workdir_has_one_commit_and_a_clean_tree(tmp_path: Path, level: str) -> None:
    sealed = wd.make_workdir(tmp_path / "run", level)
    assert _git(sealed.path, "rev-list", "--count", "HEAD").strip() == "1"
    assert _git(sealed.path, "status", "--porcelain") == ""
    assert _git(sealed.path, "rev-parse", "HEAD").strip() == sealed.commit


@pytest.mark.parametrize("level", LEVELS)
def test_sealed_workdir_holds_no_heldout_or_reference_file(tmp_path: Path, level: str) -> None:
    sealed = wd.make_workdir(tmp_path / "run", level)
    assert wd.foreign_files(sealed.path) == []
    tracked = set(_git(sealed.path, "ls-files").splitlines())
    fixture = wd.tree_files(wd.FIXTURE)
    assert fixture <= tracked
    extra = tracked - fixture
    assert extra and all(p.startswith((".specflo/", "docs/projects/")) for p in extra)
    for name in tracked:
        assert not name.startswith("bench/")
        assert "fixture-levels" not in name and "heldout" not in name


def test_foreign_files_names_a_planted_heldout_file(tmp_path: Path) -> None:
    sealed = wd.make_workdir(tmp_path / "run", "quick")
    planted = sealed.path / "tests" / "test_heldout_quick.py"
    planted.write_text("def test_x():\n    pass\n")
    assert wd.foreign_files(sealed.path) == ["tests/test_heldout_quick.py"]


@pytest.mark.parametrize("level", LEVELS)
def test_status_shows_active_project_at_level_carrying_its_request(
    tmp_path: Path, level: str
) -> None:
    sealed = wd.make_workdir(tmp_path / "run", level)
    status = json.loads(_specflo(sealed.path, "status", "--json"))
    assert status["active_project"] == sealed.slug
    assert status["level"] == level
    assert f"Level: {level}" in _specflo(sealed.path, "status")
    doc = _specflo(sealed.path, "doc", "show", wd.REQUEST_ARTIFACT[level])
    request = (wd.LEVELS_DIR / f"{level}.md").read_text(encoding="utf-8")
    for line in request.splitlines():
        text = line.lstrip("#").strip()
        if text:
            assert text in doc, text
    title = request.splitlines()[0].lstrip("#").strip()
    assert title in _specflo(sealed.path, "doc", "show", "project")


@pytest.mark.parametrize("inside", ["", "bench", "bench/fixture/work"])
def test_destination_inside_the_repo_is_refused(inside: str) -> None:
    dest = wd.REPO / inside / "sealed-run-should-not-exist"
    with pytest.raises(wd.WorkdirError, match="outside the repo"):
        wd.make_workdir(dest, "quick")
    assert not dest.exists()


def test_existing_nonempty_destination_is_refused(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "x").write_text("x")
    with pytest.raises(wd.WorkdirError, match="not empty"):
        wd.make_workdir(tmp_path / "run", "quick")


def test_unknown_level_is_refused(tmp_path: Path) -> None:
    with pytest.raises(wd.WorkdirError, match="level"):
        wd.make_workdir(tmp_path / "run", "harden")
