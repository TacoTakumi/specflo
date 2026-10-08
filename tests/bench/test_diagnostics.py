"""Diagnostics and contamination flags read from a normalised session log."""

from modelbench import diagnostics, normlog

WORKDIR = "/runs/w1"
BENCH = "/repo/bench"
ARCHIVE = "/store/heldout.tar.gz"
INITIAL = {".", "src", "tests"}


def _log(*calls: tuple, harness: str = "pi") -> dict:
    """Build a log from (name, arguments, is_error) tuples, one second apart."""
    tool_calls = [
        {
            "id": f"t{i}",
            "request_id": "r1",
            "time": 1001.0 + i,
            "name": name,
            "arguments": args,
            "is_error": err,
            "result_size": 10,
        }
        for i, (name, args, err) in enumerate(calls)
    ]
    log = {
        "format": normlog.FORMAT_VERSION,
        "harness": harness,
        "requests": [
            {
                "id": "r1",
                "start": 1000.0,
                "end": 1000.5,
                "input_tokens": 10,
                "cached_tokens": 0,
                "output_tokens": 5,
                "request_class": "main",
            }
        ],
        "tool_calls": tool_calls,
    }
    normlog.check(log)
    return log


def _run(log: dict, complete: bool = True) -> dict:
    return diagnostics.diagnose(
        log,
        workdir=WORKDIR,
        protected=[BENCH, ARCHIVE],
        initial_dirs=INITIAL,
        complete=complete,
    )


def _flags(result: dict) -> list[str]:
    return [f["flag"] for f in result["flags"]]


CLEAN = [
    ("read", {"path": "src/app.py"}, False),
    ("bash", {"command": "mkdir -p src/pkg/sub"}, False),
    ("write", {"path": "src/pkg/sub/mod.py", "content": "x"}, False),
    ("edit", {"path": "/runs/w1/src/app.py", "oldText": "a", "newText": "b"}, False),
    ("bash", {"command": "uv run pytest"}, True),
    ("bash", {"command": "ls"}, True),
    ("bash", {"command": "ls"}, True),
    ("bash", {"command": "uv run pytest -q"}, False),
]


def test_clean_log_raises_nothing():
    result = _run(_log(*CLEAN))
    assert result == {"flags": [], "contaminated": False, "contamination": []}


def test_clean_claude_code_log_raises_nothing():
    log = _log(
        ("Read", {"file_path": "/runs/w1/src/app.py"}, False),
        ("Write", {"file_path": "/runs/w1/tests/test_new.py", "content": "x"}, False),
        ("Glob", {"pattern": "**/*.py", "path": "/runs/w1"}, False),
        ("Bash", {"command": "uv run python -m pytest"}, False),
        harness="claude-code",
    )
    assert _flags(_run(log)) == []
    assert not _run(log)["contaminated"]


def test_write_outside_workdir():
    log = _log(*CLEAN, ("write", {"path": "../other/x.py", "content": "x"}, False))
    result = _run(log)
    assert _flags(result) == ["write-outside-workdir"]
    assert result["flags"][0]["path"] == "/runs/other/x.py"
    assert not result["contaminated"]


def test_claude_code_write_outside_workdir():
    log = _log(("Edit", {"file_path": "/etc/hosts"}, False), harness="claude-code")
    assert _flags(_run(log)) == ["write-outside-workdir"]


def test_write_into_missing_directory():
    log = _log(*CLEAN, ("write", {"path": "lib/new/x.py", "content": "x"}, False))
    result = _run(log)
    assert _flags(result) == ["write-missing-dir"]
    assert result["flags"][0]["path"] == "/runs/w1/lib/new/x.py"


def test_second_write_into_the_same_new_directory_is_not_flagged_again():
    log = _log(
        ("write", {"path": "lib/a.py"}, False),
        ("write", {"path": "lib/b.py"}, False),
    )
    assert _flags(_run(log)) == ["write-missing-dir"]


def test_three_identical_failing_calls_in_a_row():
    bad = ("bash", {"command": "make build"}, True)
    log = _log(*CLEAN, bad, bad, bad, bad)
    result = _run(log)
    assert _flags(result) == ["repeated-failing-call"]
    assert result["flags"][0]["count"] == 4


def test_two_identical_failing_calls_or_a_broken_streak_is_not_flagged():
    bad = ("bash", {"command": "make build"}, True)
    other = ("bash", {"command": "make build -j2"}, True)
    assert _flags(_run(_log(bad, bad, other, bad))) == []


def test_complete_project_whose_last_test_run_failed():
    log = _log(*CLEAN, ("bash", {"command": "uv run pytest tests/"}, True))
    assert _flags(_run(log, complete=True)) == ["complete-tests-failing"]


def test_incomplete_project_whose_last_test_run_failed_is_not_flagged():
    log = _log(*CLEAN, ("bash", {"command": "uv run pytest tests/"}, True))
    assert _flags(_run(log, complete=False)) == []


def test_read_of_the_archive_is_contaminated():
    log = _log(*CLEAN, ("read", {"path": ARCHIVE}, False))
    result = _run(log)
    assert result["contaminated"] is True
    assert result["contamination"] == [{"call_id": "t8", "tool": "read", "path": ARCHIVE}]
    assert _flags(result) == []


def test_read_of_the_bench_directory_is_contaminated():
    log = _log(
        ("Grep", {"pattern": "def test", "path": "/repo/bench/fixture"}, False),
        harness="claude-code",
    )
    assert _run(log)["contaminated"] is True


def test_bash_command_naming_a_protected_path_is_contaminated():
    for command in (
        "tar tzf /store/heldout.tar.gz",
        "cat ../../repo/bench/arms.yaml",
        "python -c \"print(open('/repo/bench/mb.py').read())\"",
    ):
        log = _log(("bash", {"command": command}, False))
        assert _run(log)["contaminated"] is True, command


def test_similar_but_unprotected_paths_are_not_contaminated():
    log = _log(
        ("bash", {"command": "ls /repo/bench2 /store/heldout.tar.gz.bak"}, False),
        ("read", {"path": "bench/notes.md"}, False),
    )
    assert _run(log)["contaminated"] is False


def test_a_call_naming_the_runs_own_run_dir_is_not_contaminated():
    own = "/repo/bench/runs/arm--pi--fast--001"
    log = _log(
        ("bash", {"command": f"ls {own}/pi-agent; readlink -f $(readlink -f {own}/bin/specflo)"}, False),
        ("read", {"path": f"{own}/pi-agent/settings.json"}, False),
    )
    result = diagnostics.diagnose(log, workdir=WORKDIR, protected=[BENCH, ARCHIVE], own_dirs=[own],
                                  initial_dirs=INITIAL, complete=True)
    assert result["contaminated"] is False


def test_another_runs_dir_and_the_bench_stay_protected_beside_the_runs_own_dir():
    own = "/repo/bench/runs/arm--pi--fast--001"
    for command in ("cat /repo/bench/runs/arm--pi--fast--000/record.json", "ls /repo/bench/fixture",
                    f"ls {own} /repo/bench/archive"):
        log = _log(("bash", {"command": command}, False))
        result = diagnostics.diagnose(log, workdir=WORKDIR, protected=[BENCH, ARCHIVE], own_dirs=[own],
                                      initial_dirs=INITIAL, complete=True)
        assert result["contaminated"] is True, command


def test_snapshot_dirs_lists_the_tree(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "a.py").write_text("")
    assert diagnostics.snapshot_dirs(tmp_path) == {".", "src", "src/pkg"}
