"""Tests for the locked() advisory-lock context manager (src/specflo/locking.py).

Covers REQ-01..REQ-04: stdlib-only cross-platform lock file at an explicitly
given path, full read-compute-write span, never unlink the lock file,
bounded-wait contention with a loud SpecfloError on timeout.
"""

import ast
import inspect
import multiprocessing
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from specflo import locking
from specflo.errors import SpecfloError

from conftest import executable_identifiers


def test_lock_file_is_exactly_the_given_path_and_target_untouched(tmp_path):
    target = tmp_path / "artifact.md"
    lock_path = tmp_path / "artifact.md.lock"
    assert not target.exists()
    with locking.locked(lock_path):
        assert lock_path.exists()
    # locking never creates or writes the artifact itself, and never unlinks
    # the lock file (it persists after release, by design).
    assert not target.exists()
    assert lock_path.exists()


def test_locked_creates_missing_parent_dirs(tmp_path):
    lock_path = tmp_path / "locks" / "proj" / "artifact.md.lock"
    assert not lock_path.parent.exists()
    with locking.locked(lock_path):
        assert lock_path.exists()
    assert lock_path.exists()


def test_second_process_blocked_until_release_then_sees_full_write(tmp_path):
    """A second process's whole critical section is excluded while the holder
    runs its read-compute-write; once the holder releases, the waiter enters
    and reads the fully-written artifact (REQ-02: lock spans the write)."""
    target = tmp_path / "artifact.md"
    lock_path = tmp_path / "artifact.md.lock"
    target.write_text("")

    held = multiprocessing.Event()
    release = multiprocessing.Event()
    entered = multiprocessing.Event()
    out = multiprocessing.Queue()

    def holder():
        with locking.locked(lock_path):
            held.set()
            release.wait(10)
            # read-compute-write inside the critical section
            doc = target.read_text()
            target.write_text(doc + "holder-entry\n")
        out.put("holder-done")

    def waiter():
        held.wait(10)
        t0 = time.monotonic()
        with locking.locked(lock_path):
            waited = time.monotonic() - t0
            content = target.read_text()
            entered.set()
            out.put(("waited", waited, "holder-entry" in content))
        out.put("waiter-done")

    p1 = multiprocessing.Process(target=holder)
    p2 = multiprocessing.Process(target=waiter)
    p1.start()
    assert held.wait(10), "holder never acquired the lock"
    p2.start()

    time.sleep(0.5)  # waiter polls at ~50ms; it must still be excluded
    assert not entered.is_set(), "waiter entered while the lock was held"

    release.set()
    assert entered.wait(10), "waiter never entered after the release"

    p1.join(10)
    p2.join(10)
    assert p1.exitcode == 0 and p2.exitcode == 0

    results = [out.get(timeout=5) for _ in range(3)]  # holder-done + waiter tuple + waiter-done
    messages = {r for r in results if isinstance(r, str)}
    assert messages == {"holder-done", "waiter-done"}
    waited = [r[1] for r in results if isinstance(r, tuple)][0]
    saw_write = [r[2] for r in results if isinstance(r, tuple)][0]
    assert waited >= 0.3, f"waiter was not genuinely blocked (waited {waited:.2f}s)"
    assert saw_write, "waiter read the artifact before the holder's write completed"


def test_lock_released_on_body_exception(tmp_path):
    lock_path = tmp_path / "artifact.md.lock"
    with pytest.raises(RuntimeError):
        with locking.locked(lock_path):
            raise RuntimeError("boom")
    # a subsequent acquisition must succeed: the lock was released
    with locking.locked(lock_path, timeout=1.0):
        pass


def test_timeout_raises_specflo_error_naming_lock_path(tmp_path):
    lock_path = tmp_path / "artifact.md.lock"
    with locking.locked(lock_path):
        with pytest.raises(SpecfloError) as ei:
            with locking.locked(lock_path, timeout=0.2):
                pass
        assert "artifact.md.lock" in str(ei.value)
        assert "specflo" in str(ei.value).lower()


def test_held_lock_blocks_a_second_handle_in_same_process(tmp_path):
    """Two open file descriptions on the same lock file contend even within
    one process (flock binds to the open file, not the process): a held flock
    excludes a second locked() acquisition, which times out loudly; once the
    first handle is released, a fresh acquisition succeeds."""
    fcntl = pytest.importorskip("fcntl")
    lock_path = tmp_path / "artifact.md.lock"
    fd = open(lock_path, "a+b")
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(SpecfloError):
            with locking.locked(lock_path, timeout=0.2):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    # the lock is not wedged: a fresh acquisition succeeds
    with locking.locked(lock_path, timeout=1.0):
        pass


def test_default_timeout_is_around_ten_seconds():
    assert locking.LOCK_TIMEOUT == pytest.approx(10.0)


def test_module_never_unlinks_renames_or_replaces_the_lock_path():
    code = executable_identifiers(locking)
    for banned in ("unlink", "remove", "rename"):
        assert banned not in code, f"locking module must never call {banned}()"


def test_module_imports_only_stdlib_and_specflo():
    tree = ast.parse(inspect.getsource(locking))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"specflo"}
    assert imported <= allowed, f"non-stdlib imports: {imported - allowed}"


def test_degrades_to_unlocked_with_warning_when_no_lock_api(tmp_path, monkeypatch):
    monkeypatch.setattr(locking, "_fcntl", None)
    monkeypatch.setattr(locking, "_msvcrt", None)
    lock_path = tmp_path / "artifact.md.lock"
    with pytest.warns(RuntimeWarning):
        with locking.locked(lock_path):
            pass
    # no lock file is even created on the degrade path
    assert not lock_path.exists()


def test_lock_path_for_maps_artifact_into_config_locks_tree(tmp_path):
    artifact = tmp_path / "docs" / "projects" / "myproj" / "brainstorm.md"
    p = locking.lock_path_for(tmp_path, "myproj", artifact)
    assert p == tmp_path / ".specflo" / "locks" / "myproj" / "brainstorm.md.lock"


def test_lock_path_for_creates_dir_tree_and_self_ignoring_gitignore(tmp_path):
    artifact = tmp_path / "docs" / "projects" / "myproj" / "spec.md"
    locking.lock_path_for(tmp_path, "myproj", artifact)
    assert (tmp_path / ".specflo" / "locks" / "myproj").is_dir()
    gitignore = tmp_path / ".specflo" / "locks" / ".gitignore"
    assert gitignore.read_text().strip() == "*"


def test_lock_path_for_restores_a_deleted_gitignore(tmp_path):
    artifact = tmp_path / "docs" / "projects" / "myproj" / "plan.md"
    locking.lock_path_for(tmp_path, "myproj", artifact)
    gitignore = tmp_path / ".specflo" / "locks" / ".gitignore"
    gitignore.unlink()
    locking.lock_path_for(tmp_path, "myproj", artifact)
    assert gitignore.read_text().strip() == "*"


def test_locks_live_under_config_dir_never_beside_artifacts(tmp_path):
    """A locked write on every target kind (brainstorm, spec, plan, auto-run
    state) puts its lock under .specflo/locks/<project>/ and leaves zero *.lock
    files anywhere in the projects dir (REQ-01, REQ-02, REQ-04)."""
    specflo_bin = str(Path(sys.executable).parent / "specflo")

    def run(*args):
        r = subprocess.run(
            [specflo_bin, *args], capture_output=True, text=True, cwd=tmp_path
        )
        assert r.returncode == 0, r.stderr

    run("init")
    run("new", "Loc")
    run("brainstorm", "start")
    run("decision", "add", "--text", "d1")
    b_md = tmp_path / "docs" / "projects" / "loc" / "brainstorm.md"
    b_md.write_text(
        b_md.read_text().replace(
            "## Out of scope / Deferred\n<!-- required, must be non-empty before validate passes -->",
            "## Out of scope / Deferred\n- nothing here",
        )
    )
    run("advance")
    run("spec", "start")
    run("requirement", "add", "--text", "r1", "--acceptance", "a1", "--from", "D-01")
    spec_md = tmp_path / "docs" / "projects" / "loc" / "spec.md"
    spec_md.write_text(
        spec_md.read_text()
        .replace("### In scope\n<!-- required, non-empty -->", "### In scope\n- x")
        .replace(
            "### Out of scope\n"
            "<!-- required, non-empty; carried from the brainstorm's Out of scope / Deferred -->",
            "### Out of scope\n- y",
        )
    )
    run("advance")
    run("plan", "start")
    run("task", "add", "--text", "t1", "--acceptance", "a1", "--verify", "v1", "--from", "REQ-01")

    # auto-run state is the fourth locked target kind; save it in-process
    from specflo import auto, config

    cfg = config.load_config(tmp_path)
    auto.save_run_state(tmp_path, cfg, "loc", {"passes": 1})

    project_dir = tmp_path / "docs" / "projects" / "loc"
    assert (project_dir / "auto-run.json").exists()
    assert list((tmp_path / "docs" / "projects").rglob("*.lock")) == []
    locks = tmp_path / ".specflo" / "locks" / "loc"
    for name in ("brainstorm.md.lock", "spec.md.lock", "plan.md.lock", "auto-run.json.lock"):
        assert (locks / name).exists(), f"missing {name} under .specflo/locks/loc/"


def test_concurrent_add_and_transition_lose_no_entries(tmp_path):
    """A `task add` racing a `task start` on the same plan.md loses nothing:
    the appended T-02 entry survives and T-01's progress flips (REQ-06
    behavioral). Without the lock the transition's write would clobber the
    just-appended entry (lost write)."""
    specflo_bin = str(Path(sys.executable).parent / "specflo")

    def run(*args):
        r = subprocess.run(
            [specflo_bin, *args], capture_output=True, text=True, cwd=tmp_path
        )
        assert r.returncode == 0, r.stderr

    run("init")
    run("new", "Race")
    run("brainstorm", "start")
    run("decision", "add", "--text", "d1")
    b_md = tmp_path / "docs" / "projects" / "race" / "brainstorm.md"
    b_text = b_md.read_text()
    b_md.write_text(
        b_text.replace(
            "## Out of scope / Deferred\n<!-- required, must be non-empty before validate passes -->",
            "## Out of scope / Deferred\n- nothing here",
        )
        if "## Out of scope / Deferred" in b_text
        else b_text + "\n## Out of scope / Deferred\n- nothing here\n"
    )
    run("advance")
    run("spec", "start")
    run("requirement", "add", "--text", "r1", "--acceptance", "a1", "--from", "D-01")
    spec_md = tmp_path / "docs" / "projects" / "race" / "spec.md"
    text = spec_md.read_text()
    spec_md.write_text(
        text.replace("### In scope\n<!-- required, non-empty -->", "### In scope\n- x")
        .replace(
            "### Out of scope\n"
            "<!-- required, non-empty; carried from the brainstorm's Out of scope / Deferred -->",
            "### Out of scope\n- y",
        )
    )
    run("advance")
    run("plan", "start")
    run("task", "add", "--text", "t1", "--acceptance", "a1", "--verify", "v1", "--from", "REQ-01")

    procs = [
        subprocess.Popen(
            [specflo_bin, "task", "add", "--text", "t2", "--acceptance", "a2",
             "--verify", "v2", "--from", "REQ-01"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tmp_path,
        ),
        subprocess.Popen(
            [specflo_bin, "task", "start", "T-01"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tmp_path,
        ),
    ]
    for p in procs:
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, err

    doc = (tmp_path / "docs" / "projects" / "race" / "plan.md").read_text()
    assert "### T-02 — t2" in doc, "the concurrent add's entry was lost"
    assert "- Progress: in_progress" in doc, "the transition's update was lost"


def test_posix_prefers_fcntl_over_msvcrt():
    if sys.platform == "win32":
        assert locking._msvcrt is not None
        assert locking._fcntl is None
    else:
        assert locking._fcntl is not None
        assert locking._msvcrt is None


def test_concurrent_cli_adds_mint_distinct_sequential_ids(tmp_path):
    """Eight concurrent `specflo decision add` processes on one artifact mint
    D-01..D-08 with no duplicates and no lost entries (REQ-05 behavioral).
    Drives the real installed console script, one OS process per add, so this
    exercises the exact read-modify-write hazard the lock exists to fix."""
    specflo_bin = str(Path(sys.executable).parent / "specflo")

    def run(*args):
        r = subprocess.run(
            [specflo_bin, *args], capture_output=True, text=True, cwd=tmp_path
        )
        assert r.returncode == 0, r.stderr

    run("init")
    run("new", "Concurrency")
    run("brainstorm", "start")

    procs = [
        subprocess.Popen(
            [specflo_bin, "decision", "add", "--text", f"decision {i}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=tmp_path,
        )
        for i in range(8)
    ]
    for p in procs:
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, err

    doc = (tmp_path / "docs" / "projects" / "concurrency" / "brainstorm.md").read_text()
    ids = re.findall(r"^### (D-\d+) —", doc, re.MULTILINE)
    # no duplicates, complete, sequential - the lock's guarantee. The ID-to-text
    # assignment is by lock-acquisition (execution) order, not launch order
    # (D-04), so each authored text must be present, but under any ID.
    assert ids == [f"D-{i:02d}" for i in range(1, 9)]
    titles = re.findall(r"^### D-\d+ — (.*)$", doc, re.MULTILINE)
    assert set(titles) == {f"decision {i}" for i in range(8)}
    assert doc.count("### D-") == 8
