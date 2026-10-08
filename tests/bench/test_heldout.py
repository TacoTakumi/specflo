"""The held-out archive: pack/unpack, nothing held out left in plain text, and each level's suite.

A level test reads its suite and reference solution from the committed
archive, grades a fixture copy with the suite in a separate directory, and
expects a failure on the clean fixture and a full pass on the solution.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from modelbench import heldout

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "bench"
FIXTURE = BENCH / "fixture"
ARCHIVE = BENCH / "heldout.tar.gz"
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sample_tree(root: Path) -> Path:
    _write(root, "quick/tests/test_a.py", "def test_a():\n    assert True\n")
    _write(root, "quick/solution/pkg/mod.py", "X = 1\n")
    _write(root, "fast/tests/sub/test_b.py", "def test_b():\n    pass\n")
    return root


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# Archive tool


def test_pack_unpack_round_trip(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path / "src")
    archive = tmp_path / "h.tar.gz"
    heldout.pack(src, archive)
    heldout.unpack(archive, tmp_path / "out")
    assert _tree(tmp_path / "out") == _tree(src)
    assert heldout.list_levels(archive) == ["fast", "quick"]
    assert heldout.level_files(archive, "quick", "tests") == {
        "test_a.py": b"def test_a():\n    assert True\n"
    }
    assert heldout.level_files(archive, "fast", "solution") == {}


def test_pack_is_deterministic(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path / "src")
    heldout.pack(src, tmp_path / "a.tar.gz")
    os.utime(src / "quick/solution/pkg/mod.py", (1, 1))
    (src / "fast/tests/sub/test_b.py").chmod(0o600)
    heldout.pack(src, tmp_path / "b.tar.gz")
    assert (tmp_path / "a.tar.gz").read_bytes() == (tmp_path / "b.tar.gz").read_bytes()


def test_repack_after_adding_a_level(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path / "src")
    archive = tmp_path / "h.tar.gz"
    heldout.pack(src, archive)
    work = tmp_path / "work"
    heldout.unpack(archive, work)
    _write(work, "full/tests/test_c.py", "def test_c():\n    pass\n")
    heldout.pack(work, archive)
    assert heldout.list_levels(archive) == ["fast", "full", "quick"]


def test_pack_rejects_a_stray_top_level_file(tmp_path: Path) -> None:
    src = _sample_tree(tmp_path / "src")
    _write(src, "notes.txt", "x\n")
    with pytest.raises(heldout.HeldoutError):
        heldout.pack(src, tmp_path / "h.tar.gz")


def _evil_archive(path: Path, info: tarfile.TarInfo, data: bytes = b"") -> Path:
    with tarfile.open(path, "w:gz") as tar:
        info.size = len(data) if info.isfile() else 0
        tar.addfile(info, io.BytesIO(data) if info.isfile() else None)
    return path


@pytest.mark.parametrize(
    "name", ["/etc/x", "../x", "quick/../../x", "quick/tests/../../../x"]
)
def test_unpack_refuses_unsafe_paths(tmp_path: Path, name: str) -> None:
    archive = _evil_archive(tmp_path / "e.tar.gz", tarfile.TarInfo(name), b"x")
    with pytest.raises(heldout.HeldoutError):
        heldout.unpack(archive, tmp_path / "out")
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_unpack_refuses_links(tmp_path: Path, kind: bytes) -> None:
    info = tarfile.TarInfo("quick/tests/link.py")
    info.type = kind
    info.linkname = "/etc/passwd"
    archive = _evil_archive(tmp_path / "e.tar.gz", info)
    with pytest.raises(heldout.HeldoutError):
        heldout.unpack(archive, tmp_path / "out")
    assert not (tmp_path / "out" / "quick" / "tests" / "link.py").exists()


# Nothing held out in plain text


def _repo_files() -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        found.extend(Path(dirpath) / f for f in filenames)
    return found


def test_no_plain_text_heldout_file_in_repo() -> None:
    members: dict[str, bytes] = {}
    for level in heldout.list_levels(ARCHIVE):
        for part in heldout.PARTS:
            for rel, data in heldout.level_files(ARCHIVE, level, part).items():
                members[f"{level}/{part}/{rel}"] = data
    assert members
    suite_names = {Path(m).name for m in members if m.split("/")[1] == "tests"}
    digests = {hashlib.sha256(d).hexdigest(): m for m, d in members.items()}
    sizes = {len(d) for d in members.values()}
    leaks = []
    for path in _repo_files():
        if path == ARCHIVE or not path.is_file() or path.is_symlink():
            continue
        if path.name in suite_names:
            leaks.append(f"{path}: held-out suite file name")
        elif path.stat().st_size in sizes:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest in digests:
                leaks.append(f"{path}: same bytes as {digests[digest]}")
    assert not leaks, leaks


# Level suites: one helper per level, selected with -k <level>


def _pytest(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Drop the outer run's pytest and xdist settings so the graded tree uses its own config.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=100,
    )


def grade_level(level: str, tmp_path: Path, *, solved: bool) -> subprocess.CompletedProcess[str]:
    """Copy the fixture (plus the level's solution when solved) and run its held-out suite."""
    if solved:
        work = heldout.apply_solution(ARCHIVE, level, FIXTURE, tmp_path / "work")
    else:
        work = heldout.copy_fixture(FIXTURE, tmp_path / "work")
    suite = heldout.extract_level(ARCHIVE, level, "tests", tmp_path / "grading")
    return _pytest(work, "--rootdir", str(work), "-c", str(work / "pyproject.toml"), str(suite))


def assert_level_fails_clean(level: str, tmp_path: Path) -> None:
    proc = grade_level(level, tmp_path, solved=False)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert " failed" in proc.stdout


def assert_level_passes_solved(level: str, tmp_path: Path) -> None:
    proc = grade_level(level, tmp_path, solved=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert " passed" in proc.stdout
    assert "failed" not in proc.stdout and "error" not in proc.stdout


def assert_solution_keeps_visible_suite(level: str, tmp_path: Path) -> None:
    work = heldout.apply_solution(ARCHIVE, level, FIXTURE, tmp_path / "work")
    proc = _pytest(work)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_quick_is_in_archive() -> None:
    assert "quick" in heldout.list_levels(ARCHIVE)
    assert heldout.level_files(ARCHIVE, "quick", "tests")
    assert heldout.level_files(ARCHIVE, "quick", "solution")


def test_quick_heldout_fails_on_clean_fixture(tmp_path: Path) -> None:
    assert_level_fails_clean("quick", tmp_path)


def test_quick_heldout_passes_on_reference_solution(tmp_path: Path) -> None:
    assert_level_passes_solved("quick", tmp_path)


def test_quick_solution_keeps_visible_suite_green(tmp_path: Path) -> None:
    assert_solution_keeps_visible_suite("quick", tmp_path)


def test_fast_is_in_archive() -> None:
    assert "fast" in heldout.list_levels(ARCHIVE)
    assert heldout.level_files(ARCHIVE, "fast", "tests")
    assert heldout.level_files(ARCHIVE, "fast", "solution")


def test_fast_heldout_fails_on_clean_fixture(tmp_path: Path) -> None:
    assert_level_fails_clean("fast", tmp_path)


def test_fast_heldout_passes_on_reference_solution(tmp_path: Path) -> None:
    assert_level_passes_solved("fast", tmp_path)


def test_fast_solution_keeps_visible_suite_green(tmp_path: Path) -> None:
    assert_solution_keeps_visible_suite("fast", tmp_path)
