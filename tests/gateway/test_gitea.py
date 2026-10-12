"""The Gitea client: what the gateway asks a Gitea forge, made with the bot's token.

Against a stub Gitea the client lists the issues of a repo that carry the
trigger label and changed since a time, reads an issue, its comments and the
label events of its timeline, creates and edits a comment and finds one by
id, and gives the repo's clone URL. Every request carries the bot token from
the token file, read again at each request; no fault names it, and the clone
URL carries no credential.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from specflo.gateway import config, forge, gitea

from .stub_forge import StubGitea

TOKEN = "fake-gitea-token-c41d"
REPO = "me/thing"
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


def _forge(token_file, url="https://gitea.example"):
    return config.Forge(
        name="gitea", kind=config.GITEA, url=url, repos=(REPO,), operator_id=OPERATOR,
        pool="rfc-research", token_file=token_file,
    )


def _run(stub, token_file, ask, url="https://gitea.example"):
    """Run ``ask(client)`` against *stub* with a client that is closed after."""

    async def main():
        async with gitea.GiteaClient(_forge(token_file, url), transport=stub.transport()) as client:
            return await ask(client)

    return asyncio.run(main())


def test_the_client_is_a_forge_client(token_file, stub):
    client = gitea.GiteaClient(_forge(token_file), transport=stub.transport())
    try:
        assert isinstance(client, forge.ForgeClient)
    finally:
        asyncio.run(client.aclose())


def test_it_lists_the_labelled_issues_that_changed_since_a_time(token_file, stub):
    stub.add_issue(REPO, 1, title="Old one", labels=("rfc",))
    since = stub.tick()
    stub.add_issue(REPO, 2, title="Not labelled")
    stub.add_issue(REPO, 3, title="Other label", labels=("bug",))
    stub.add_issue(REPO, 4, title="A pull request", labels=("rfc",), pull=True)
    stub.add_issue(REPO, 5, title="Closed", labels=("rfc",), state="closed")
    for number in (6, 7, 8):
        stub.add_issue(REPO, number, title=f"Issue {number}", body=f"Body {number}")
        stub.tick()
        stub.label(REPO, number, "rfc", OPERATOR)

    issues = _run(stub, token_file, lambda c: c.labelled_issues(REPO, "rfc", since))

    # Three issues on a stub that pages two at a time: the client reads every page.
    assert sorted(issue.number for issue in issues) == [6, 7, 8]
    six = next(issue for issue in issues if issue.number == 6)
    assert six.repo == REPO
    assert six.title == "Issue 6"
    assert six.body == "Body 6"
    assert six.author_id == OPERATOR
    assert six.labels == ("rfc",)
    assert six.updated_at == stub.issues[(REPO, 6)].updated_at
    assert six.url == "https://gitea.example/me/thing/issues/6"

    asked = [seen for seen in stub.requests if seen.path.endswith("/repos/me/thing/issues")]
    assert asked[0].params["labels"] == "rfc"
    assert asked[0].params["type"] == "issues"
    assert asked[0].params["state"] == "open"
    assert datetime.fromisoformat(asked[0].params["since"]) == since


def test_without_a_time_it_lists_every_labelled_issue(token_file, stub):
    stub.add_issue(REPO, 1, labels=("rfc",))
    stub.tick()
    stub.add_issue(REPO, 2, labels=("rfc",))

    issues = _run(stub, token_file, lambda c: c.labelled_issues(REPO, "rfc"))

    assert sorted(issue.number for issue in issues) == [1, 2]
    assert all("since" not in seen.params for seen in stub.requests)


def test_a_time_without_a_zone_is_refused(token_file, stub):
    stub.add_issue(REPO, 1, labels=("rfc",))
    with pytest.raises(ValueError):
        _run(stub, token_file, lambda c: c.labelled_issues(REPO, "rfc", datetime(2026, 1, 1)))


def test_it_reads_an_issue(token_file, stub):
    stub.add_issue(REPO, 12, title="Why X", body="Because Y", author_id=STRANGER, labels=("rfc", "bug"))

    issue = _run(stub, token_file, lambda c: c.issue(REPO, 12))

    assert (issue.repo, issue.number, issue.title, issue.body) == (REPO, 12, "Why X", "Because Y")
    assert issue.author_id == STRANGER
    assert issue.labels == ("rfc", "bug")


def test_it_reads_an_issues_comments_oldest_first(token_file, stub):
    stub.add_issue(REPO, 3)
    first = stub.add_comment(REPO, 3, OPERATOR, "first")
    stub.tick()
    second = stub.add_comment(REPO, 3, STRANGER, "second")
    stub.add_issue(REPO, 4)
    stub.add_comment(REPO, 4, OPERATOR, "on another issue")

    comments = _run(stub, token_file, lambda c: c.comments(REPO, 3))

    assert [(c.id, c.author_id, c.body) for c in comments] == [
        (first, OPERATOR, "first"),
        (second, STRANGER, "second"),
    ]
    assert comments[1].created_at == stub.now
    assert comments[0].url.endswith(f"#issuecomment-{first}")


def test_it_reads_the_label_events_of_an_issues_timeline(token_file, stub):
    stub.add_issue(REPO, 5)
    stub.tick()
    stub.label(REPO, 5, "rfc", OPERATOR)
    on = stub.now
    stub.add_comment(REPO, 5, STRANGER, "a comment is no label event")
    stub.tick()
    stub.unlabel(REPO, 5, "rfc", STRANGER)
    off = stub.now
    stub.tick()
    stub.label(REPO, 5, "bug", STRANGER)

    events = _run(stub, token_file, lambda c: c.label_events(REPO, 5))

    # Four timeline entries on a stub that pages two at a time.
    assert events == [
        forge.LabelEvent(label="rfc", actor_id=OPERATOR, at=on, added=True),
        forge.LabelEvent(label="rfc", actor_id=STRANGER, at=off, added=False),
        forge.LabelEvent(label="bug", actor_id=STRANGER, at=stub.now, added=True),
    ]


def test_it_creates_edits_and_finds_a_comment(token_file, stub):
    stub.add_issue(REPO, 6)

    async def ask(client):
        made = await client.create_comment(REPO, 6, "Working on it.")
        found = await client.find_comment(REPO, made.id)
        edited = await client.edit_comment(REPO, made.id, "The report.")
        return made, found, edited

    made, found, edited = _run(stub, token_file, ask)

    assert made.author_id == stub.bot_id
    assert made.body == "Working on it."
    assert found == made
    assert edited.id == made.id
    assert edited.body == "The report."
    assert stub.comment_body(made.id) == "The report."
    posted = [seen for seen in stub.requests if seen.method in ("POST", "PATCH")]
    assert [(seen.method, seen.body) for seen in posted] == [
        ("POST", {"body": "Working on it."}),
        ("PATCH", {"body": "The report."}),
    ]


def test_a_deleted_comment_is_not_found(token_file, stub):
    stub.add_issue(REPO, 6)
    gone = stub.add_comment(REPO, 6, stub.bot_id, "Working on it.")
    stub.delete_comment(gone)

    assert _run(stub, token_file, lambda c: c.find_comment(REPO, gone)) is None


def test_the_clone_url_carries_no_credential(token_file, stub):
    async def ask(client):
        return client.clone_url(REPO)

    assert _run(stub, token_file, ask) == "https://gitea.example/me/thing.git"
    assert _run(stub, token_file, ask, url="https://gitea.example/git") == (
        "https://gitea.example/git/me/thing.git"
    )
    with_user = _run(stub, token_file, ask, url="https://bot:hunter2@gitea.example:3000")
    assert with_user == "https://gitea.example:3000/me/thing.git"
    assert stub.requests == []


def test_every_request_carries_the_bot_token(token_file, stub):
    stub.add_issue(REPO, 1, labels=("rfc",))
    stub.label(REPO, 1, "rfc", OPERATOR)
    stub.add_comment(REPO, 1, OPERATOR, "more")

    async def ask(client):
        await client.labelled_issues(REPO, "rfc", stub.now - timedelta(hours=1))
        await client.issue(REPO, 1)
        await client.comments(REPO, 1)
        await client.label_events(REPO, 1)
        made = await client.create_comment(REPO, 1, "Working on it.")
        await client.edit_comment(REPO, made.id, "Done.")
        await client.find_comment(REPO, made.id)

    _run(stub, token_file, ask)

    assert len(stub.requests) >= 7
    assert {seen.headers["authorization"] for seen in stub.requests} == {f"token {TOKEN}"}


def test_the_token_is_read_again_at_each_request(token_file, stub):
    stub.add_issue(REPO, 1)

    async def ask(client):
        await client.issue(REPO, 1)
        token_file.write_text("fake-gitea-token-rotated\n", encoding="utf-8")
        stub.token = "fake-gitea-token-rotated"
        await client.issue(REPO, 1)

    _run(stub, token_file, ask)

    assert [seen.headers["authorization"] for seen in stub.requests] == [
        f"token {TOKEN}", "token fake-gitea-token-rotated",
    ]


def test_a_token_file_opened_up_is_refused_before_any_request(token_file, stub):
    stub.add_issue(REPO, 1)
    os.chmod(token_file, 0o644)

    with pytest.raises(gitea.ForgeError) as raised:
        _run(stub, token_file, lambda c: c.issue(REPO, 1))

    assert "0600" in str(raised.value)
    assert TOKEN not in str(raised.value)
    assert stub.requests == []


def test_a_refused_token_fails_without_naming_it(token_file):
    stub = StubGitea(token="fake-gitea-token-other")
    stub.add_issue(REPO, 1)

    with pytest.raises(gitea.ForgeError) as raised:
        _run(stub, token_file, lambda c: c.issue(REPO, 1))

    assert raised.value.status == 401
    assert "401" in str(raised.value)
    assert TOKEN not in str(raised.value)
    assert TOKEN not in repr(raised.value)


def test_a_missing_issue_is_a_forge_error(token_file, stub):
    stub.add_issue(REPO, 1)

    with pytest.raises(gitea.ForgeError) as raised:
        _run(stub, token_file, lambda c: c.issue(REPO, 99))

    assert raised.value.status == 404
    assert "/repos/me/thing/issues/99" in str(raised.value)


def test_a_forge_that_cannot_be_reached_is_a_forge_error(token_file):
    import httpx

    def unreachable(request):
        raise httpx.ConnectError("connection refused", request=request)

    async def main():
        transport = httpx.MockTransport(unreachable)
        async with gitea.GiteaClient(_forge(token_file), transport=transport) as client:
            await client.issue(REPO, 1)

    with pytest.raises(gitea.ForgeError) as raised:
        asyncio.run(main())

    assert raised.value.status is None
    assert TOKEN not in str(raised.value)


def test_times_from_another_zone_are_read_as_the_same_moment(token_file, stub):
    stub.add_issue(REPO, 1)
    stub.add_comment(REPO, 1, OPERATOR, "x")
    comments = _run(stub, token_file, lambda c: c.comments(REPO, 1))
    assert comments[0].created_at == stub.now
    assert comments[0].created_at.tzinfo is not None
    assert gitea.parse_time("2026-01-05T17:00:00+08:00") == datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
