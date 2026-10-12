"""Polling a Gitea forge for label events that start a run.

Against a stub Gitea a poll asks each watched repo for the issues with the
trigger label that changed since its cursor (the gateway's first start, the
first time), reads each one's timeline, and gives a trigger for an issue
whose trigger label the operator put on last. A label put on by anyone else,
another label, an event older than the first start and an event the store
has handled give none, also after a restart. The cursor moves to the newest
issue time seen, and a repo the forge refuses does not keep the others from
being polled.
"""

import asyncio
import os
from datetime import timedelta

import pytest

from specflo.gateway import config, gitea, poll, store
from specflo.gateway.forge import LabelEvent

from .stub_forge import EPOCH, StubGitea

TOKEN = "fake-gitea-token-7e2a"
REPO = "me/thing"
OTHER_REPO = "me/other"
OPERATOR = 7
STRANGER = 9


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "gitea.token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def stub():
    return StubGitea(token=TOKEN)


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "root"
    path.mkdir()
    return path


def _forge(token_file, repos=(REPO,)):
    return config.Forge(
        name="home", kind=config.GITEA, url="https://gitea.example", repos=repos,
        operator_id=OPERATOR, pool="rfc-research", token_file=token_file,
    )


def _poll(stub, token_file, state, repos=(REPO,)):
    """One poll of the stub with a client that is closed after; the triggers."""
    forge = _forge(token_file, repos)

    async def main():
        async with gitea.GiteaClient(forge, transport=stub.transport()) as client:
            return await poll.poll_gitea(forge, client, state)

    return asyncio.run(main())


def _start(stub, root):
    """The gateway's store, opened at the stub's time now, as a start does."""
    return store.open_store(root, now=stub.now)


def test_an_operator_label_gives_one_trigger_from_the_labelled_issues_and_the_timeline(
    token_file, stub, root
):
    stub.add_issue(REPO, 1)
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "rfc", OPERATOR)

    triggers = _poll(stub, token_file, state)

    assert triggers == [poll.Trigger(
        forge="home", repo=REPO, number=1, at=stub.now, actor_id=OPERATOR,
    )]
    listing, timeline = stub.requests
    assert listing.path == f"/api/v1/repos/{REPO}/issues"
    assert listing.params["labels"] == "rfc"
    assert listing.params["since"] == gitea.format_time(state.first_start)
    assert timeline.path == f"/api/v1/repos/{REPO}/issues/1/timeline"


def test_a_label_put_on_by_another_user_gives_no_trigger(token_file, stub, root):
    stub.add_issue(REPO, 1)
    stub.add_issue(REPO, 2)
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "rfc", STRANGER)
    stub.label(REPO, 2, "rfc", OPERATOR)

    triggers = _poll(stub, token_file, state)

    assert [trigger.number for trigger in triggers] == [2]


def test_only_the_label_that_is_on_the_issue_now_counts(token_file, stub, root):
    stub.add_issue(REPO, 1)
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "rfc", OPERATOR)
    stub.tick()
    stub.unlabel(REPO, 1, "rfc", STRANGER)
    stub.tick()
    stub.label(REPO, 1, "rfc", STRANGER)

    assert _poll(stub, token_file, state) == []


def test_another_label_gives_no_trigger(token_file, stub, root):
    stub.add_issue(REPO, 1, labels=("rfc",))
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "bug", OPERATOR)

    assert _poll(stub, token_file, state) == []


def test_an_event_older_than_the_first_start_gives_no_trigger(token_file, stub, root):
    stub.add_issue(REPO, 1)
    stub.label(REPO, 1, "rfc", OPERATOR)
    stub.tick()
    state = _start(stub, root)
    # Something else changes the issue after the start, so the poll sees it.
    stub.tick()
    stub.add_comment(REPO, 1, STRANGER, "Any news?")

    assert _poll(stub, token_file, state) == []


def test_a_handled_event_gives_no_trigger_again_after_a_restart(token_file, stub, root):
    stub.add_issue(REPO, 1)
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "rfc", OPERATOR)
    (trigger,) = _poll(stub, token_file, state)
    state.mark_handled(trigger)

    stub.tick()
    restarted = _start(stub, root)
    # The feed has the same event and the issue changed again since.
    stub.add_comment(REPO, 1, OPERATOR, "More context.")

    assert restarted.first_start < trigger.at
    assert _poll(stub, token_file, restarted) == []


def test_a_new_operator_label_after_a_handled_one_gives_a_new_trigger(token_file, stub, root):
    stub.add_issue(REPO, 1)
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "rfc", OPERATOR)
    (first,) = _poll(stub, token_file, state)
    state.mark_handled(first)
    stub.tick()
    stub.unlabel(REPO, 1, "rfc", OPERATOR)
    stub.tick()
    stub.label(REPO, 1, "rfc", OPERATOR)

    (second,) = _poll(stub, token_file, state)

    assert second.number == 1
    assert second.at == stub.now


def test_the_cursor_moves_to_the_newest_issue_time_seen(token_file, stub, root):
    stub.add_issue(REPO, 1)
    state = _start(stub, root)
    stub.tick()
    stub.label(REPO, 1, "rfc", OPERATOR)
    _poll(stub, token_file, state)
    newest = stub.now
    stub.requests.clear()

    stub.tick()
    _poll(stub, token_file, state)

    assert state.since("home", REPO) == newest
    assert stub.requests[0].params["since"] == gitea.format_time(newest)


def test_a_poll_with_no_labelled_issue_leaves_the_cursor(token_file, stub, root):
    stub.add_issue(REPO, 1)
    state = _start(stub, root)

    assert _poll(stub, token_file, state) == []
    assert state.since("home", REPO) is None


def test_a_repo_the_forge_refuses_does_not_stop_the_others(token_file, stub, root):
    stub.add_issue(OTHER_REPO, 1)
    state = _start(stub, root)
    stub.tick()
    stub.label(OTHER_REPO, 1, "rfc", OPERATOR)

    # The stub does not know me/gone, so it answers 404 for it.
    triggers = _poll(stub, token_file, state, repos=("me/gone", OTHER_REPO))

    assert [(trigger.repo, trigger.number) for trigger in triggers] == [(OTHER_REPO, 1)]
    assert state.since("home", "me/gone") is None


def test_the_trigger_rule_is_the_same_for_any_feed_of_label_events(root):
    state = store.open_store(root, now=EPOCH)
    forge = _forge(None)
    later = EPOCH + timedelta(minutes=1)
    events = [
        LabelEvent(label="rfc", actor_id=OPERATOR, at=later, added=True),
    ]

    found = poll.trigger_for(forge, REPO, 4, events, state)

    assert found == poll.Trigger(forge="home", repo=REPO, number=4, at=later, actor_id=OPERATOR)
    assert poll.trigger_for(forge, REPO, 4, [], state) is None
    removed = [*events, LabelEvent(label="rfc", actor_id=OPERATOR, at=later, added=False)]
    assert poll.trigger_for(forge, REPO, 4, removed, state) is None
