"""The bench fixture: a small Python repo whose visible suite passes clean, plus one request per level."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[2] / "bench"
FIXTURE = BENCH / "fixture"
LEVELS_DIR = BENCH / "fixture-levels"
LEVELS = ("quick", "fast", "full")


def fixture_levels(levels_dir: Path = LEVELS_DIR) -> list[str]:
    """Return the levels that have a non-empty request file, in quick-fast-full order."""
    return [
        level
        for level in LEVELS
        if (levels_dir / f"{level}.md").is_file()
        and (levels_dir / f"{level}.md").read_text(encoding="utf-8").strip()
    ]


def _copy_fixture(dest: Path) -> Path:
    """Copy the fixture without caches, so a run never writes into the repo."""
    target = dest / "fixture"
    shutil.copytree(
        FIXTURE,
        target,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )
    return target


def test_visible_suite_passes_on_clean_fixture(tmp_path: Path) -> None:
    work = _copy_fixture(tmp_path)
    # Drop the outer run's pytest and xdist settings so the fixture uses its own config.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=100,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert " passed" in proc.stdout
    assert "failed" not in proc.stdout


def test_fixture_is_self_contained() -> None:
    assert (FIXTURE / "pyproject.toml").is_file()
    assert (FIXTURE / "tests").is_dir()
    assert (FIXTURE / "tinytodo" / "__init__.py").is_file()
    # The level requests and anything held out live outside the copied tree.
    assert not any(p.name.endswith(".md") and p.stem in LEVELS for p in FIXTURE.rglob("*"))


def test_each_level_has_a_request() -> None:
    for level in LEVELS:
        text = (LEVELS_DIR / f"{level}.md").read_text(encoding="utf-8")
        assert text.strip(), f"{level} request is empty"


def test_fixture_check_reports_three_levels() -> None:
    assert fixture_levels() == ["quick", "fast", "full"]


def test_fixture_check_reports_missing_level(tmp_path: Path) -> None:
    (tmp_path / "quick.md").write_text("Do one thing.\n", encoding="utf-8")
    (tmp_path / "fast.md").write_text("  \n", encoding="utf-8")
    assert fixture_levels(tmp_path) == ["quick"]
