"""Grade a run's final tree against a level's held-out suite, with a pytest config the grader owns.

The grading directory is a fresh directory outside the repo and outside every
run workdir (the graded tree counts as one). It holds:

    tree/<package>/...   the `.py` files of the tree's package, and nothing else
    reference/<package>/ the fixture's package with the level's solution on top
    tests/...            the level's held-out suite, from the archive
    _grader/             the grader's pytest.ini, runner and JSON-lines reports

Only the package's `.py` files are copied, never `conftest.py`,
`sitecustomize.py` or `usercustomize.py`, and no symlink. So nothing the agent
put at the tree root (conftest.py, pytest.ini, pyproject.toml, tox.ini,
setup.cfg, .pth files, its own tests) reaches the grading run. pytest runs as
`python -I` (no PYTHONPATH, user site or script dir) with the PYTEST_* and
PYTHON* variables removed, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, `-c` on the
grader's ini, `--rootdir` on the grading dir, `--noconftest` and
`-p no:cacheprovider`. Results come from a report file a grader plugin
writes, not from the exit status.

Score = passed / total. The total is the number of tests the held-out suite
collects against the reference solution, so a tree cannot shrink it. A test
counts as passed only when its call passed and its setup and teardown did not
fail. Failed, error (setup or teardown failed), skipped (xfail too) and
missing (not run: a collection error, a missing package, a timeout) all count
as not passed. A collection error in one module scores that module's tests as
missing; other modules still run.

Limit: the tree's package is imported into the grading process, so code that
patches pytest at import time is out of reach here; the run's contamination
check is the guard for that.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from modelbench import heldout

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "bench"
FIXTURE = BENCH / "fixture"
ARCHIVE = BENCH / "heldout.tar.gz"
PACKAGE = "tinytodo"
OUTCOMES = ("passed", "failed", "error", "skipped", "missing")
DEFAULT_TIMEOUT = 600.0

_SKIP_NAMES = {"conftest.py", "sitecustomize.py", "usercustomize.py"}

_INI = """[pytest]
addopts =
python_files = test_*.py
python_classes = Test*
python_functions = test_*
console_output_style = classic
"""

# Runs inside the grading process: puts the given paths first on sys.path and
# records each collected test and each phase report as one JSON line.
_RUNNER = '''import json
import sys

conf = json.load(open(sys.argv[1], encoding="utf-8"))
sys.path[:0] = conf["paths"]
import pytest


class Recorder:
    def __init__(self, path):
        self.out = open(path, "a", encoding="utf-8")

    def _write(self, obj):
        self.out.write(json.dumps(obj) + "\\n")
        self.out.flush()

    def pytest_collection_finish(self, session):
        for item in session.items:
            self._write({"collected": item.nodeid})

    def pytest_runtest_logreport(self, report):
        self._write({
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "xfail": hasattr(report, "wasxfail"),
        })


sys.exit(pytest.main(conf["args"], plugins=[Recorder(conf["report"])]))
'''


class GraderError(RuntimeError):
    """The tree cannot be graded: bad level, bad grading dir, or a broken suite."""


@dataclass
class GradeResult:
    level: str
    score: float
    passed: int
    total: int
    counts: dict[str, int]
    outcomes: dict[str, str]
    grading_dir: str
    timed_out: bool = False
    kept: bool = False
    tree: str = field(default="")

    def to_dict(self) -> dict:
        return asdict(self)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def check_grading_dir(grading_dir: Path, workdirs: Iterable[Path]) -> Path:
    """Return the resolved grading dir, or raise if it is in the repo, in a workdir, or not empty."""
    resolved = grading_dir.resolve()
    if _inside(resolved, REPO):
        raise GraderError(f"grading dir must be outside the repo ({REPO}): {resolved}")
    for workdir in workdirs:
        root = Path(workdir).resolve()
        if _inside(resolved, root) or _inside(root, resolved):
            raise GraderError(f"grading dir must be outside every run workdir ({root}): {resolved}")
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise GraderError(f"grading dir exists and is not empty: {resolved}")
    return resolved


def copy_package(src_root: Path, package: str, dest_root: Path) -> list[str]:
    """Copy the package's `.py` files, minus conftest/site hooks and symlinks; return them."""
    src = src_root / package
    copied: list[str] = []
    dest_root.mkdir(parents=True, exist_ok=True)
    if not src.is_dir() or src.is_symlink():
        return copied
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src_root)
        if path.name in _SKIP_NAMES or "__pycache__" in rel.parts:
            continue
        if any(p.is_symlink() for p in [path, *path.parents] if _inside(p, src)):
            continue
        if not path.is_file():
            continue
        target = dest_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        copied.append(rel.as_posix())
    return copied


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "PYTHON"))}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_pytest(
    grading: Path, name: str, src: Path, extra: list[str], timeout: float
) -> tuple[list[dict], bool]:
    """Run the held-out suite against `src`; return the report lines and whether it timed out."""
    gdir = grading / "_grader"
    report = gdir / f"{name}.jsonl"
    conf = gdir / f"{name}.json"
    args = [
        "-c", str(gdir / "pytest.ini"), "--rootdir", str(grading),
        "--noconftest", "-p", "no:cacheprovider", "--continue-on-collection-errors",
        "-q", *extra, str(grading / "tests"),
    ]
    conf.write_text(
        json.dumps({"paths": [str(src)], "args": args, "report": str(report)}), encoding="utf-8"
    )
    report.write_text("", encoding="utf-8")
    timed_out = False
    try:
        subprocess.run(
            [sys.executable, "-I", str(gdir / "runner.py"), str(conf)],
            cwd=grading, env=_env(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    lines = []
    for line in report.read_text(encoding="utf-8").splitlines():
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return lines, timed_out


def _outcomes(expected: list[str], lines: list[dict]) -> dict[str, str]:
    reports: dict[str, dict[str, dict]] = {}
    for line in lines:
        if "nodeid" in line:
            reports.setdefault(line["nodeid"], {})[line["when"]] = line
    result = {}
    for nodeid in expected:
        phases = reports.get(nodeid, {})
        setup, call, teardown = (phases.get(w) for w in ("setup", "call", "teardown"))
        if any(p and p["outcome"] == "failed" for p in (setup, teardown)):
            result[nodeid] = "error"
        elif call is not None:
            if call["xfail"] or call["outcome"] == "skipped":
                result[nodeid] = "skipped"
            else:
                result[nodeid] = "passed" if call["outcome"] == "passed" else "failed"
        elif setup is not None and setup["outcome"] == "skipped":
            result[nodeid] = "skipped"
        else:
            result[nodeid] = "missing"
    return result


def grade(
    tree: str | Path,
    level: str,
    *,
    archive: str | Path = ARCHIVE,
    fixture: str | Path = FIXTURE,
    package: str = PACKAGE,
    grading_dir: str | Path | None = None,
    workdirs: Iterable[str | Path] = (),
    keep: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> GradeResult:
    """Grade `tree` against `level`'s held-out suite; see the module docstring for the rules."""
    tree = Path(tree).resolve()
    if not tree.is_dir():
        raise GraderError(f"tree is not a directory: {tree}")
    levels = heldout.list_levels(archive)
    if level not in levels:
        raise GraderError(f"unknown level {level!r}: archive has {', '.join(levels)}")
    roots = [tree, *(Path(w) for w in workdirs)]
    if grading_dir is None:
        grading = Path(tempfile.mkdtemp(prefix="mb-grade-"))
        try:
            check_grading_dir(grading, roots)
        except GraderError:
            shutil.rmtree(grading, ignore_errors=True)
            raise
    else:
        grading = check_grading_dir(Path(grading_dir), roots)
        grading.mkdir(parents=True, exist_ok=True)
    grading = grading.resolve()
    try:
        gdir = grading / "_grader"
        gdir.mkdir()
        (gdir / "pytest.ini").write_text(_INI, encoding="utf-8")
        (gdir / "runner.py").write_text(_RUNNER, encoding="utf-8")
        heldout.extract_level(archive, level, "tests", grading)

        ref = grading / "reference"
        copy_package(Path(fixture), package, ref)
        for rel, data in heldout.level_files(archive, level, "solution").items():
            if rel.split("/")[0] == package:
                (ref / rel).parent.mkdir(parents=True, exist_ok=True)
                (ref / rel).write_bytes(data)
        ref_lines, _ = _run_pytest(grading, "reference", ref, ["--collect-only"], timeout)
        expected = [line["collected"] for line in ref_lines if "collected" in line]
        if not expected:
            raise GraderError(f"the {level} held-out suite collects no tests on its reference solution")

        copy_package(tree, package, grading / "tree")
        lines, timed_out = _run_pytest(grading, "tree", grading / "tree", [], timeout)
        outcomes = _outcomes(expected, lines)
    finally:
        if not keep:
            shutil.rmtree(grading, ignore_errors=True)
    counts = {o: sum(1 for v in outcomes.values() if v == o) for o in OUTCOMES}
    total = len(expected)
    return GradeResult(
        level=level,
        score=counts["passed"] / total,
        passed=counts["passed"],
        total=total,
        counts=counts,
        outcomes=outcomes,
        grading_dir=str(grading),
        timed_out=timed_out,
        kept=keep,
        tree=str(tree),
    )
