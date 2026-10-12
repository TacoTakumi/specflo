"""The GitHub client: what the gateway asks a GitHub forge, as the forge's GitHub App.

Against a stub GitHub the client does what the Gitea client does, through
githubkit's async client on the App's narrowed installation token: it lists
the open issues of a repo that carry the trigger label and changed since a
time, reads an issue, its comments and its label events, creates and edits a
comment and finds one by id, and gives the repo's clone URL, with no
credential in it. The token to clone with is one of its own, which may only
read the repo's contents. A failed call names the call and the status, and
never a token.
"""

import asyncio
import os

import httpx
import pytest

from specflo.gateway import config, forge, github

from .stub_forge import APP_ID, StubGitHub, app_key

TOKEN = "ghs_fake-installation-token-9b3e"
REPO = "me/thing"
OPERATOR = 7
STRANGER = 9


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "github-app.pem"
    path.write_text(app_key()[0], encoding="ascii")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def stub():
    return StubGitHub(token=TOKEN)


def _forge(key_file, url=config.GITHUB_API):
    return config.Forge(
        name="github", kind=config.GITHUB, url=url, repos=(REPO,), operator_id=OPERATOR,
        pool="rfc-research", app_id=APP_ID, key_file=key_file,
    )


def _run(stub, key_file, ask, url=config.GITHUB_API):
    """Run ``ask(client)`` against *stub* with a client that is closed after."""

    async def main():
        client = github.GitHubClient(_forge(key_file, url), transport=stub.transport())
        try:
            return await ask(client)
        finally:
            await client.aclose()

    return asyncio.run(main())


def test_the_client_is_a_forge_client(key_file, stub):
    client = github.GitHubClient(_forge(key_file), transport=stub.transport())
    try:
        assert isinstance(client, forge.ForgeClient)
    finally:
        asyncio.run(client.aclose())


def test_it_lists_the_open_labelled_issues_that_changed_since_a_time(key_file, stub):
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

    issues = _run(stub, key_file, lambda c: c.labelled_issues(REPO, "rfc", since))

    # Three issues and a pull request on a stub that pages two at a time.
    assert sorted(issue.number for issue in issues) == [6, 7, 8]
    six = next(issue for issue in issues if issue.number == 6)
    assert (six.repo, six.title, six.body) == (REPO, "Issue 6", "Body 6")
    assert six.author_id == OPERATOR
    assert six.labels == ("rfc",)
    assert six.updated_at == stub.issues[(REPO, 6)].updated_at
    assert six.url == "https://github.com/me/thing/issues/6"
    asked = [seen for seen in stub.requests if seen.path == "/repos/me/thing/issues"]
    assert asked[0].params["labels"] == "rfc" and asked[0].params["state"] == "open"
    assert "since" in asked[0].params


def test_it_reads_an_issue(key_file, stub):
    stub.add_issue(
        REPO, 12, title="Why X", body="Because Y", author_id=STRANGER, labels=("rfc", "bug"),
    )

    issue = _run(stub, key_file, lambda c: c.issue(REPO, 12))

    assert (issue.repo, issue.number, issue.title, issue.body) == (REPO, 12, "Why X", "Because Y")
    assert issue.author_id == STRANGER
    assert issue.labels == ("rfc", "bug")


def test_it_reads_an_issues_comments_oldest_first_over_pages(key_file, stub):
    stub.add_issue(REPO, 3)
    made = []
    for text in ("first", "second", "third"):
        made.append(stub.add_comment(REPO, 3, OPERATOR, text))
        stub.tick()

    comments = _run(stub, key_file, lambda c: c.comments(REPO, 3))

    assert [(c.id, c.body) for c in comments] == list(zip(made, ["first", "second", "third"]))
    assert comments[0].author_id == OPERATOR
    assert comments[0].url.endswith(f"#issuecomment-{made[0]}")


def test_it_reads_the_label_events_of_an_issue(key_file, stub):
    stub.add_issue(REPO, 5)
    stub.tick()
    stub.label(REPO, 5, "rfc", OPERATOR)
    on = stub.now
    stub.tick()
    stub.unlabel(REPO, 5, "rfc", STRANGER)
    off = stub.now
    stub.tick()
    stub.label(REPO, 5, "bug", STRANGER)

    events = _run(stub, key_file, lambda c: c.label_events(REPO, 5))

    assert events == [
        forge.LabelEvent(label="rfc", actor_id=OPERATOR, at=on, added=True),
        forge.LabelEvent(label="rfc", actor_id=STRANGER, at=off, added=False),
        forge.LabelEvent(label="bug", actor_id=STRANGER, at=stub.now, added=True),
    ]


def test_it_creates_edits_and_finds_a_comment(key_file, stub):
    stub.add_issue(REPO, 6)

    async def ask(client):
        made = await client.create_comment(REPO, 6, "Working on it.")
        found = await client.find_comment(REPO, made.id)
        edited = await client.edit_comment(REPO, made.id, "The report.")
        return made, found, edited

    made, found, edited = _run(stub, key_file, ask)

    assert made.author_id == stub.bot_id and made.body == "Working on it."
    assert found == made
    assert edited.id == made.id and edited.body == "The report."
    assert stub.comment_body(made.id) == "The report."
    posted = [seen for seen in stub.requests if seen.method in ("POST", "PATCH")
              and "/access_tokens" not in seen.path]
    assert [(seen.method, seen.path, seen.body) for seen in posted] == [
        ("POST", "/repos/me/thing/issues/6/comments", {"body": "Working on it."}),
        ("PATCH", f"/repos/me/thing/issues/comments/{made.id}", {"body": "The report."}),
    ]


def test_a_deleted_comment_is_not_found_and_its_edit_is_a_404(key_file, stub):
    stub.add_issue(REPO, 6)
    gone = stub.add_comment(REPO, 6, stub.bot_id, "Working on it.")
    stub.delete_comment(gone)

    assert _run(stub, key_file, lambda c: c.find_comment(REPO, gone)) is None
    with pytest.raises(forge.ForgeError) as raised:
        _run(stub, key_file, lambda c: c.edit_comment(REPO, gone, "x"))
    assert raised.value.status == 404


def test_every_repo_call_carries_the_installation_token(key_file, stub):
    stub.add_issue(REPO, 1, labels=("rfc",))
    stub.label(REPO, 1, "rfc", OPERATOR)

    async def ask(client):
        await client.labelled_issues(REPO, "rfc")
        await client.issue(REPO, 1)
        await client.comments(REPO, 1)
        await client.label_events(REPO, 1)
        made = await client.create_comment(REPO, 1, "Working on it.")
        await client.edit_comment(REPO, made.id, "Done.")
        await client.find_comment(REPO, made.id)

    _run(stub, key_file, ask)

    repo_calls = [seen for seen in stub.requests if seen.path.startswith("/repos/me/thing/issues")]
    assert len(repo_calls) >= 7
    assert {seen.headers["authorization"] for seen in repo_calls} == {f"token {TOKEN}"}
    # One token for all of them, held to issues and contents.
    assert stub.token_bodies == [
        {"permissions": {"issues": "write", "contents": "read"}, "repositories": ["thing"]},
    ]


def test_the_clone_url_carries_no_credential(key_file, stub):
    async def ask(client):
        return client.clone_url(REPO)

    assert _run(stub, key_file, ask) == "https://github.com/me/thing.git"
    assert _run(stub, key_file, ask, url="https://ghe.example/api/v3") == (
        "https://ghe.example/me/thing.git"
    )
    assert _run(stub, key_file, ask, url="https://bot:hunter2@ghe.example/api/v3/") == (
        "https://ghe.example/me/thing.git"
    )
    assert stub.requests == []


def test_the_clone_token_may_only_read_the_repos_contents(key_file, stub):
    stub.add_issue(REPO, 1)

    token = _run(stub, key_file, lambda c: c.clone_token(REPO))

    assert token == TOKEN
    assert stub.token_bodies == [{"permissions": {"contents": "read"}, "repositories": ["thing"]}]


def test_a_missing_issue_is_a_forge_error_naming_the_call(key_file, stub):
    stub.add_issue(REPO, 1)

    with pytest.raises(forge.ForgeError) as raised:
        _run(stub, key_file, lambda c: c.issue(REPO, 99))

    assert raised.value.status == 404
    assert "/repos/me/thing/issues/99" in str(raised.value)
    assert TOKEN not in str(raised.value)
    assert raised.value.__cause__ is None


def test_a_refused_token_is_a_forge_error_without_secrets(key_file):
    stub = StubGitHub(token=TOKEN)
    stub.add_issue(REPO, 1)

    def refuse_tokens(request):
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(401, json={"message": "Bad credentials"})
        return stub.handle(request)

    async def main():
        client = github.GitHubClient(_forge(key_file), transport=httpx.MockTransport(refuse_tokens))
        try:
            await client.issue(REPO, 1)
        finally:
            await client.aclose()

    with pytest.raises(forge.ForgeError) as raised:
        asyncio.run(main())

    assert raised.value.status == 401
    assert "me/thing" in str(raised.value)
    assert "Bearer" not in str(raised.value)
