"""The gateway's store: its first start, the label events it handled and its poll cursors.

The store is one JSON file in the daemon root's ``gateway`` dir. The first
open records the time the gateway first started, and every later open keeps
it. A handled label event is recorded per issue, so a restart knows it, and
an event on that issue that is not newer than the recorded one counts as
handled. Each repo's poll cursor is kept the same way. Every write replaces
the file whole, so a write that fails leaves the old file; a file that cannot
be read starts the store afresh and is kept aside.
"""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from specflo.gateway import poll, store

START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def _trigger(at, number=1, actor_id=7, forge="gitea", repo="me/thing"):
    return poll.Trigger(forge=forge, repo=repo, number=number, at=at, actor_id=actor_id)


def test_the_first_open_records_the_first_start_in_the_daemon_root(tmp_path):
    opened = store.open_store(tmp_path, now=START)

    assert opened.first_start == START
    assert opened.path == tmp_path / "gateway" / "state.json"
    assert json.loads(opened.path.read_text(encoding="utf-8"))["first_start"]


def test_a_later_open_keeps_the_first_start(tmp_path):
    store.open_store(tmp_path, now=START)

    again = store.open_store(tmp_path, now=START + timedelta(days=3))

    assert again.first_start == START


def test_a_handled_event_stays_handled_after_a_restart(tmp_path):
    first = store.open_store(tmp_path, now=START)
    event = _trigger(START + timedelta(minutes=5))
    assert not first.is_handled(event)

    first.mark_handled(event)

    again = store.open_store(tmp_path, now=START + timedelta(hours=1))
    assert again.is_handled(event)


def test_an_older_event_on_a_handled_issue_counts_as_handled_and_a_newer_one_does_not(tmp_path):
    opened = store.open_store(tmp_path, now=START)
    opened.mark_handled(_trigger(START + timedelta(minutes=5)))

    assert opened.is_handled(_trigger(START + timedelta(minutes=1)))
    assert not opened.is_handled(_trigger(START + timedelta(minutes=9)))


def test_handled_events_are_kept_per_forge_repo_and_issue(tmp_path):
    opened = store.open_store(tmp_path, now=START)
    at = START + timedelta(minutes=5)
    opened.mark_handled(_trigger(at))

    assert not opened.is_handled(_trigger(at, number=2))
    assert not opened.is_handled(_trigger(at, repo="me/other"))
    assert not opened.is_handled(_trigger(at, forge="github"))


def test_the_poll_cursor_of_each_repo_survives_a_restart(tmp_path):
    first = store.open_store(tmp_path, now=START)
    assert first.since("gitea", "me/thing") is None
    cursor = START + timedelta(minutes=3)

    first.set_since("gitea", "me/thing", cursor)

    again = store.open_store(tmp_path, now=START + timedelta(hours=1))
    assert again.since("gitea", "me/thing") == cursor
    assert again.since("gitea", "me/other") is None
    assert again.since("github", "me/thing") is None


def test_a_write_replaces_the_file_whole_and_leaves_no_scratch_file(tmp_path, monkeypatch):
    opened = store.open_store(tmp_path, now=START)
    opened.mark_handled(_trigger(START + timedelta(minutes=5)))
    before = opened.path.read_text(encoding="utf-8")

    def fail(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store.os, "replace", fail)
    with pytest.raises(OSError):
        opened.mark_handled(_trigger(START + timedelta(minutes=9), number=2))

    assert opened.path.read_text(encoding="utf-8") == before
    assert os.listdir(opened.path.parent) == [opened.path.name]


def test_a_store_that_cannot_be_read_starts_afresh_and_is_kept_aside(tmp_path):
    path = tmp_path / "gateway" / "state.json"
    path.parent.mkdir()
    path.write_text("{not json", encoding="utf-8")
    now = START + timedelta(days=1)

    opened = store.open_store(tmp_path, now=now)

    # A fresh first start acts on no event before now, so nothing the lost
    # file recorded is handled twice.
    assert opened.first_start == now
    assert (path.parent / "state.json.bad").read_text(encoding="utf-8") == "{not json"
    assert store.open_store(tmp_path).first_start == now


def test_a_naive_time_is_refused(tmp_path):
    with pytest.raises(ValueError):
        store.open_store(tmp_path, now=datetime(2026, 1, 5, 9, 0))
