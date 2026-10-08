"""The grader: a final tree graded against a level's held-out suite with a grader-owned config."""

from __future__ import annotations

import io
import json
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from modelbench import cmd_grade, grader, heldout

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "bench"
FIXTURE = BENCH / "fixture"
ARCHIVE = BENCH / "heldout.tar.gz"
LEVELS = ("quick", "fast", "full")


def _clean_tree(tmp_path: Path) -> Path:
    return heldout.copy_fixture(FIXTURE, tmp_path / "tree")


def _grade(tree: Path, level: str, tmp_path: Path, **kw) -> grader.GradeResult:
    return grader.grade(tree, level, grading_dir=tmp_path / "grading", **kw)


# Real levels


@pytest.mark.parametrize("level", LEVELS)
def test_reference_solution_scores_one(tmp_path: Path, level: str) -> None:
    tree = heldout.apply_solution(ARCHIVE, level, FIXTURE, tmp_path / "tree")
    result = _grade(tree, level, tmp_path)
    assert result.score == 1.0
    assert result.passed == result.total > 0


@pytest.mark.parametrize("level", LEVELS)
def test_clean_fixture_scores_below_one(tmp_path: Path, level: str) -> None:
    result = _grade(_clean_tree(tmp_path), level, tmp_path)
    assert 0.0 <= result.score < 1.0
    assert result.counts["failed"] > 0


# Planted files that try to force a pass

FORCE_PASS_CONFTEST = '''
import pytest

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    report.outcome = "passed"
    report.longrepr = None

def pytest_collection_modifyitems(config, items):
    items[:] = [i for i in items if "unchanged" in i.name]
'''

PLANTS = {
    "root-conftest": {"conftest.py": FORCE_PASS_CONFTEST},
    "package-conftest": {"tinytodo/conftest.py": FORCE_PASS_CONFTEST},
    "tests-conftest": {"tests/conftest.py": FORCE_PASS_CONFTEST},
    "pytest-ini": {"pytest.ini": "[pytest]\naddopts = -k unchanged -p no:terminal\n"},
    "pyproject": {
        "pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-k unchanged"\n',
    },
    "tox-ini": {"tox.ini": "[pytest]\naddopts = -k unchanged\n"},
    "setup-cfg": {"setup.cfg": "[tool:pytest]\naddopts = -k unchanged\n"},
    "sitecustomize": {
        "sitecustomize.py": "import os\nos.environ['PYTEST_ADDOPTS'] = '-k unchanged'\n",
        "tinytodo/sitecustomize.py": "raise SystemExit(0)\n",
        "usercustomize.py": "raise SystemExit(0)\n",
    },
    "pth-file": {"tinytodo/evil.pth": "import os; os._exit(0)\n"},
}


def _plant(tree: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_planted_files_leave_the_clean_score_unchanged(tmp_path: Path, plant: str) -> None:
    baseline = _grade(_clean_tree(tmp_path / "a"), "quick", tmp_path / "a")
    tree = _clean_tree(tmp_path / "b")
    _plant(tree, PLANTS[plant])
    planted = _grade(tree, "quick", tmp_path / "b")
    assert planted.score == baseline.score < 1.0
    assert planted.outcomes == baseline.outcomes


def test_planted_conftest_does_not_lift_a_full_level_score(tmp_path: Path) -> None:
    baseline = _grade(_clean_tree(tmp_path / "a"), "full", tmp_path / "a")
    tree = _clean_tree(tmp_path / "b")
    _plant(tree, {"conftest.py": FORCE_PASS_CONFTEST, "tinytodo/conftest.py": FORCE_PASS_CONFTEST})
    assert _grade(tree, "full", tmp_path / "b").score == baseline.score


# The grading directory


def test_grading_dir_is_outside_the_workdir_and_repo(tmp_path: Path) -> None:
    tree = _clean_tree(tmp_path)
    result = grader.grade(tree, "quick")
    graded = Path(result.grading_dir).resolve()
    assert tree.resolve() not in graded.parents and graded != tree.resolve()
    assert REPO not in graded.parents
    assert not graded.exists(), "grading dir is removed unless kept"


def test_grading_dir_inside_the_tree_is_refused(tmp_path: Path) -> None:
    tree = _clean_tree(tmp_path)
    with pytest.raises(grader.GraderError, match="workdir"):
        grader.grade(tree, "quick", grading_dir=tree / "grading")


def test_grading_dir_inside_another_workdir_is_refused(tmp_path: Path) -> None:
    tree = _clean_tree(tmp_path)
    other = tmp_path / "other-run"
    other.mkdir()
    with pytest.raises(grader.GraderError, match="workdir"):
        grader.grade(tree, "quick", grading_dir=other / "g", workdirs=[other])


def test_grading_dir_inside_the_repo_is_refused(tmp_path: Path) -> None:
    with pytest.raises(grader.GraderError, match="repo"):
        grader.grade(_clean_tree(tmp_path), "quick", grading_dir=REPO / "bench" / "grading-x")
    assert not (REPO / "bench" / "grading-x").exists()


def test_grading_dir_that_is_not_empty_is_refused(tmp_path: Path) -> None:
    busy = tmp_path / "grading"
    busy.mkdir()
    (busy / "x").write_text("x", encoding="utf-8")
    with pytest.raises(grader.GraderError, match="not empty"):
        grader.grade(_clean_tree(tmp_path), "quick", grading_dir=busy)


def test_kept_grading_dir_holds_only_the_package_and_suite(tmp_path: Path) -> None:
    tree = _clean_tree(tmp_path)
    _plant(tree, PLANTS["root-conftest"] | PLANTS["package-conftest"])
    result = _grade(tree, "quick", tmp_path, keep=True)
    graded = Path(result.grading_dir)
    copied = sorted(p.relative_to(graded / "tree").as_posix() for p in (graded / "tree").rglob("*.py"))
    assert copied == sorted(
        p.relative_to(FIXTURE).as_posix() for p in (FIXTURE / "tinytodo").rglob("*.py")
    )
    assert (graded / "tests").is_dir()


def test_unknown_level_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(grader.GraderError, match="level"):
        _grade(_clean_tree(tmp_path), "huge", tmp_path)


# Counting, on a synthetic level


SYNTH_TESTS = '''
import pytest
from pkg import VALUE

@pytest.fixture
def broken():
    raise RuntimeError("setup fails")

def test_pass():
    assert True

def test_fail_until_solved():
    assert VALUE == 2

def test_skip():
    pytest.skip("not today")

def test_error(broken):
    pass

@pytest.mark.parametrize("n", [1, 2])
def test_param(n):
    assert n <= VALUE
'''


def _synthetic(tmp_path: Path) -> tuple[Path, Path]:
    fixture = tmp_path / "fx"
    _plant(fixture, {"pkg/__init__.py": "VALUE = 1\n", "pyproject.toml": "[project]\nname='pkg'\n"})
    src = tmp_path / "src"
    _plant(src, {"lv/tests/test_lv.py": SYNTH_TESTS, "lv/solution/pkg/__init__.py": "VALUE = 2\n"})
    archive = tmp_path / "h.tar.gz"
    heldout.pack(src, archive)
    return fixture, archive


def _grade_synth(tree: Path, tmp_path: Path, fixture: Path, archive: Path) -> grader.GradeResult:
    return grader.grade(
        tree, "lv", archive=archive, fixture=fixture, package="pkg", grading_dir=tmp_path / "g"
    )


def test_counting_failures_skips_and_errors(tmp_path: Path) -> None:
    fixture, archive = _synthetic(tmp_path)
    tree = heldout.copy_fixture(fixture, tmp_path / "tree")
    result = _grade_synth(tree, tmp_path, fixture, archive)
    assert result.total == 6
    assert result.counts == {"passed": 2, "failed": 2, "skipped": 1, "error": 1, "missing": 0}
    assert result.score == pytest.approx(2 / 6)
    assert result.outcomes["tests/test_lv.py::test_param[2]"] == "failed"


def test_collection_error_scores_zero(tmp_path: Path) -> None:
    fixture, archive = _synthetic(tmp_path)
    tree = heldout.copy_fixture(fixture, tmp_path / "tree")
    (tree / "pkg" / "__init__.py").write_text("VALUE = (\n", encoding="utf-8")
    result = _grade_synth(tree, tmp_path, fixture, archive)
    assert (result.score, result.total, result.counts["missing"]) == (0.0, 6, 6)


def test_missing_package_scores_zero(tmp_path: Path) -> None:
    fixture, archive = _synthetic(tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    assert _grade_synth(tree, tmp_path, fixture, archive).score == 0.0


def test_denominator_ignores_tests_the_tree_drops(tmp_path: Path) -> None:
    # The total comes from the reference run, so a tree cannot shrink it.
    fixture, archive = _synthetic(tmp_path)
    tree = heldout.copy_fixture(fixture, tmp_path / "tree")
    shutil.rmtree(tree / "pkg")
    result = _grade_synth(tree, tmp_path, fixture, archive)
    assert result.total == 6 and result.passed == 0


# The command


def test_cmd_grade_prints_json(tmp_path: Path) -> None:
    tree = heldout.apply_solution(ARCHIVE, "quick", FIXTURE, tmp_path / "tree")
    out = io.StringIO()
    with redirect_stdout(out):
        code = cmd_grade.main(["--tree", str(tree), "--level", "quick"])
    assert code == 0
    data = json.loads(out.getvalue())
    assert data["score"] == 1.0
    assert data["level"] == "quick"
    assert isinstance(data["score"], float) and data["total"] == data["passed"]


def test_cmd_grade_unknown_level_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cmd_grade.main(["--tree", str(_clean_tree(tmp_path)), "--level", "huge"])
    assert code == 1
    assert "level" in capsys.readouterr().err
