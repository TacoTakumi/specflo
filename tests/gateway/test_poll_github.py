"""Polling a GitHub forge for label events that start a run, with ETags.

Against a stub GitHub a poll reads each watched repo's issue events feed,
newest first, back to the repo's cursor (the gateway's first start, the
first time). It groups the label events by issue and applies the same
trigger rule as the Gitea poll: a trigger for an issue whose trigger label
the operator put on last, none for anyone else's label, another label, a
pull request, an event older than the first start or one the store has
handled, also after a restart.

The next poll of the repo sends ``If-None-Match`` with the ETag of the
feed's first page. GitHub answers 304 when nothing changed, which costs no
rate limit for an authenticated request, and the poll asks that repo
nothing more until the next poll. The ETags are kept in the client's
memory, so a restart reads the feed once in full and the store keeps it from
handling an event twice.
"""

import asyncio
import os

import pytest

from specflo.gateway import config, github, poll, store

from .stub_forge import APP_ID, StubGitHub, app_key

TOKEN = "ghs_fake-installation-token-41aa"
REPO = "me/thing"
OTHER_REPO = "me/other"
OPERATOR = 7
STRANGER = 9
FEED = f"/repos/{REPO}/issues/events"


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "github-app.pem"
    path.write_text(app_key()[0], encoding="ascii")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def stub():
    return StubGitHub(token=TOKEN)


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "root"
    path.mkdir()
    return path


def _forge(key_file, repos=(REPO,)):
    return config.Forge(
        name="hub", kind=config.GITHUB, url=config.GITHUB_API, repos=repos,
        operator_id=OPERATOR, pool="rfc-research", app_id=APP_ID, key_file=key_file,
    )


def _polls(stub, key_file, state, times=1, repos=(REPO,), between=None):
    """*times* polls of the stub through one client, closed after; the triggers of each."""
    forge = _forge(key_file, repos)

    async def main():
        client = github.GitHubClient(forge, transport=stub.transport())
        found = []
        try:
            for index in range(times):
                if index and between is not None:
                    between()
                found.append(await poll.poll_github(forge, client, state))
        finally:
            await client.aclose()
        return found

    return asyncio.run(main())


def _poll(stub, key_file, state, repos=(REPO,)):
    return _polls(stub, key_file, state, repos=repos)[0]


def _start(stub, root):
    """The gateway's store, opened at the stub's time now, as a start does."""
    return store.open_store(root, now=stub.now)


def test_an_operator_label_gives_one_trigger_and_another_users_none(key_file, stub, root):
    stub.add_issue(REPO, 1)
    stub.add_issue(REPO, 2)
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "rfc", STRANGER)
    stub.label(REPO, 2, "rfc", OPERATOR)

    triggers = _poll(stub, key_file, state)

    assert triggers == [poll.Trigger(
        forge="hub", repo=REPO, number=2, at=stub.now, actor_id=OPERATOR,
    )]
    asked = [seen.path for seen in stub.requests if seen.path.startswith("/repos/me/thing/issues")]
    assert asked == [FEED]


def test_the_second_poll_sends_the_etag_and_after_a_304_asks_the_repo_nothing_more(
    key_file, stub, root
):
    stub.add_issue(REPO, 1)
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "rfc", OPERATOR)
    marks = []

    first, second = _polls(
        stub, key_file, state, times=2, between=lambda: marks.append(len(stub.requests)),
    )

    assert len(first) == 1 and second == []
    before, after = stub.requests[:marks[0]], stub.requests[marks[0]:]
    (answer,) = [seen for seen in before if seen.path == FEED]
    assert "if-none-match" not in answer.headers
    # The second poll is one request, the feed with the ETag, answered 304.
    (asked,) = after
    assert asked.path == FEED
    answers = [answer for answer in stub.answers if answer.path == FEED]
    assert asked.headers["if-none-match"] == answers[0].etag
    assert [answer.status for answer in answers] == [200, 304]


def test_a_new_event_after_a_304_is_read_and_triggers(key_file, stub, root):
    stub.add_issue(REPO, 1)
    stub.add_issue(REPO, 2)
    state = _start(stub, root)
    polls = iter([
        lambda: None,
        lambda: (stub.tick(), stub.label(REPO, 2, "rfc", OPERATOR)),
    ])

    found = _polls(stub, key_file, state, times=3, between=lambda: next(polls)())

    assert found[0] == [] and found[1] == []
    assert [trigger.number for trigger in found[2]] == [2]


def test_the_feed_is_read_over_pages_back_to_the_cursor(key_file, stub, root):
    for number in (1, 2, 3, 4, 5):
        stub.add_issue(REPO, number)
    state = _start(stub, root)
    stub.tick()
    for number in (1, 2, 3, 4, 5):
        stub.label(REPO, number, "rfc", OPERATOR)

    triggers = _poll(stub, key_file, state)

    # Five events on a stub that pages two at a time.
    assert [trigger.number for trigger in triggers] == [1, 2, 3, 4, 5]
    assert state.since("hub", REPO) == stub.now


def test_paging_stops_at_the_first_event_older_than_the_cursor(key_file, stub, root):
    for number in (1, 2, 3, 4, 5):
        stub.add_issue(REPO, number)
        stub.label(REPO, number, "bug", STRANGER)
    stub.tick()
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "rfc", OPERATOR)

    triggers = _poll(stub, key_file, state)

    assert [trigger.number for trigger in triggers] == [1]
    pages = [seen for seen in stub.requests if seen.path == FEED]
    assert len(pages) == 1


def test_only_the_label_that_is_on_the_issue_now_counts(key_file, stub, root):
    stub.add_issue(REPO, 1)
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "rfc", OPERATOR)
    stub.tick()
    stub.unlabel(REPO, 1, "rfc", STRANGER)
    stub.tick()
    stub.label(REPO, 1, "rfc", STRANGER)

    assert _poll(stub, key_file, state) == []


def test_another_label_or_a_pull_request_gives_no_trigger(key_file, stub, root):
    stub.add_issue(REPO, 1)
    stub.add_issue(REPO, 2, pull=True)
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "bug", OPERATOR)
    stub.label(REPO, 2, "rfc", OPERATOR)

    assert _poll(stub, key_file, state) == []


def test_an_event_older_than_the_first_start_gives_no_trigger(key_file, stub, root):
    stub.add_issue(REPO, 1)
    stub.label(REPO, 1, "rfc", OPERATOR)
    stub.tick()
    state = _start(stub, root)

    assert _poll(stub, key_file, state) == []


def test_a_handled_event_gives_no_trigger_again_after_a_restart(key_file, stub, root):
    stub.add_issue(REPO, 1)
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "rfc", OPERATOR)
    (trigger,) = _poll(stub, key_file, state)
    state.mark_handled(trigger)

    stub.tick()
    restarted = _start(stub, root)
    # A new client has no ETag, so the feed is read in full again.
    assert restarted.first_start < trigger.at
    assert _poll(stub, key_file, restarted) == []


def test_a_new_operator_label_after_a_handled_one_gives_a_new_trigger(key_file, stub, root):
    stub.add_issue(REPO, 1)
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "rfc", OPERATOR)

    (first,) = _poll(stub, key_file, state)
    state.mark_handled(first)
    stub.tick()
    stub.unlabel(REPO, 1, "rfc", OPERATOR)
    stub.tick()
    stub.label(REPO, 1, "rfc", OPERATOR)

    (second,) = _poll(stub, key_file, state)

    assert second.number == 1 and second.at == stub.now


def test_a_repo_the_forge_refuses_does_not_stop_the_others(key_file, stub, root):
    stub.add_issue(OTHER_REPO, 1)
    state = _start(stub, root)
    stub.tick()
    stub.label(OTHER_REPO, 1, "rfc", OPERATOR)

    # The stub does not know me/gone, so it answers 404 for it.
    triggers = _poll(stub, key_file, state, repos=("me/gone", OTHER_REPO))

    assert [(trigger.repo, trigger.number) for trigger in triggers] == [(OTHER_REPO, 1)]
    assert state.since("hub", "me/gone") is None
