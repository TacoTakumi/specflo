"""The chat log: one durable, append-only transcript per hosted project.

Every entry the daemon logs for a project (a user line from any seat, an
assistant message, a state change) lands here with an id that only grows,
a time, a kind, an author, and text. The log outlives the daemon process:
a restart reads on from the same file and keeps minting ids after the last
one written.
"""

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from specflo import daemon
from specflo.daemon import chatlog
from specflo.errors import SpecfloError


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


def test_append_returns_entries_with_ids_that_only_grow(root):
    log = chatlog.open_log(root, "login-fix")

    first = log.append("user", "requester", "Hello")
    second = log.append("assistant", "project-login-fix", "Hi, what are we building?")
    third = log.append("state", "", "working")

    assert [first.id, second.id, third.id] == [1, 2, 3]
    assert (first.kind, first.author, first.text) == ("user", "requester", "Hello")
    assert (second.kind, second.author) == ("assistant", "project-login-fix")
    assert (third.kind, third.author, third.text) == ("state", "", "working")
    for entry in (first, second, third):
        assert entry.time.endswith("+00:00") and "T" in entry.time
    assert log.last_id == 3


def test_read_from_returns_the_entries_after_an_id_in_order(root):
    log = chatlog.open_log(root, "login-fix")
    entries = [log.append("user", "requester", f"line {n}") for n in range(1, 6)]

    assert log.read_from(0) == entries
    assert log.read_from(2) == entries[2:]
    assert log.read_from(5) == []
    assert log.read_from(99) == []
    assert chatlog.open_log(root, "login-fix").read_from(3) == entries[3:]


def test_tail_reads_only_what_landed_since_the_offset_and_leaves_a_torn_line(root):
    log = chatlog.open_log(root, "login-fix")
    for n in range(3):
        log.append("user", "requester", f"line {n}")

    entries, offset = log.tail(0, 0)
    assert [e.text for e in entries] == ["line 0", "line 1", "line 2"]
    assert offset == log.path.stat().st_size

    again, same = log.tail(entries[-1].id, offset)
    assert again == [] and same == offset

    log.append("assistant", "agent", "reply")
    with open(log.path, "ab") as handle:
        handle.write(b'{"id": 5, "time": "t", "kind": "user", "author": "requester", "text": "torn')
    entries, offset = log.tail(3, offset)
    assert [e.text for e in entries] == ["reply"]
    assert log.path.read_bytes()[offset:].startswith(b'{"id": 5')

    entries, past_the_end = log.tail(0, log.path.stat().st_size + 100)
    assert [e.text for e in entries] == ["line 0", "line 1", "line 2", "reply"]
    assert past_the_end == offset

    missing = chatlog.open_log(root, "nothing-yet")
    assert missing.tail(0, 0) == ([], 0)


def test_the_log_is_one_file_per_project_under_the_root(root):
    chatlog.open_log(root, "login-fix").append("user", "requester", "one")
    chatlog.open_log(root, "dark-mode").append("user", "requester", "two")

    assert chatlog.log_path(root, "login-fix") == root / chatlog.CHAT_DIRNAME / "login-fix.jsonl"
    assert sorted(p.name for p in (root / chatlog.CHAT_DIRNAME).iterdir()) == ["dark-mode.jsonl", "login-fix.jsonl"]
    assert [e.text for e in chatlog.open_log(root, "login-fix").read_from(0)] == ["one"]
    assert [e.text for e in chatlog.open_log(root, "dark-mode").read_from(0)] == ["two"]
    line = json.loads(chatlog.log_path(root, "login-fix").read_text().splitlines()[0])
    assert set(line) == {"id", "time", "kind", "author", "text"}


def test_an_empty_or_missing_log_reads_as_nothing_and_starts_at_one(root):
    log = chatlog.open_log(root, "login-fix")

    assert log.read_from(0) == []
    assert log.last_id == 0
    assert not chatlog.log_path(root, "login-fix").exists()
    assert log.append("state", "", "idle").id == 1


def test_a_slug_that_is_not_one_names_no_log(root):
    with pytest.raises(SpecfloError):
        chatlog.open_log(root, "../etc")
    assert not (root / chatlog.CHAT_DIRNAME).exists()


APPEND_IN_ANOTHER_PROCESS = """
import sys
from pathlib import Path
from specflo.daemon import chatlog
entry = chatlog.open_log(Path(sys.argv[1]), sys.argv[2]).append("user", "developer", sys.argv[3])
print(entry.id)
"""


def test_entries_survive_a_process_restart_and_ids_continue(root):
    log = chatlog.open_log(root, "login-fix")
    log.append("user", "requester", "before")
    log.append("assistant", "project-login-fix", "reply")

    run = subprocess.run(
        [sys.executable, "-c", APPEND_IN_ANOTHER_PROCESS, str(root), "login-fix", "from the pane"],
        capture_output=True, text=True, timeout=60,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "3"
    chatlog.forget_open_logs()
    reopened = chatlog.open_log(root, "login-fix")
    assert [(e.id, e.author, e.text) for e in reopened.read_from(0)] == [
        (1, "requester", "before"),
        (2, "project-login-fix", "reply"),
        (3, "developer", "from the pane"),
    ]
    assert reopened.append("state", "", "idle").id == 4


def test_a_torn_last_line_is_skipped_and_the_id_continues_from_the_last_whole_entry(root):
    log = chatlog.open_log(root, "login-fix")
    log.append("user", "requester", "whole")
    with open(chatlog.log_path(root, "login-fix"), "a") as handle:
        handle.write('{"id": 2, "time": "2026-09-07T00:00:00+00:00", "kind": "user", "au')
    chatlog.forget_open_logs()

    reopened = chatlog.open_log(root, "login-fix")

    assert [e.id for e in reopened.read_from(0)] == [1]
    assert reopened.append("user", "requester", "next").id == 2
    assert [e.text for e in chatlog.open_log(root, "login-fix").read_from(0)] == ["whole", "next"]


def test_concurrent_appends_mint_distinct_sequential_ids(root):
    log = chatlog.open_log(root, "login-fix")
    go = threading.Barrier(8)

    def append(n):
        go.wait()
        return log.append("user", "requester", f"line {n}").id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = sorted(pool.map(append, range(8)))

    assert ids == list(range(1, 9))
    assert [e.id for e in log.read_from(0)] == ids


def test_open_log_hands_back_the_same_log_for_the_same_project(root):
    assert chatlog.open_log(root, "login-fix") is chatlog.open_log(root, "login-fix")
    assert chatlog.open_log(root, "login-fix") is not chatlog.open_log(root, "dark-mode")
