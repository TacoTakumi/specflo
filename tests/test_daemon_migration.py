"""The older tokens file is moved into the state store once, on first start.

Before named users, the daemon kept one bearer token per mint in
``<root>/tokens.json``, each bound to requester, developer or agent. The
first start of a daemon that still has the file carries its agent tokens
into the state store as tokens of the built-in agent, under the same hash,
so a seat or a script holding one keeps working. The requester and developer
tokens are dropped: a person now gets a personal token of their own. The
file is then renamed aside, so a second start finds nothing to do, and
``specflo serve token add`` is gone.
"""

import hashlib
import json
import logging
import secrets

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import config, daemon, guide
from specflo.cli import app
from specflo.daemon import auth, routes
from specflo.daemon.app import create_app
from specflo.daemon.store import Token, open_store
from specflo.errors import SpecfloError

runner = CliRunner()


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


def write_legacy(root, *identities, created="2026-09-06"):
    """Write a tokens file the way the older mint did; each identity's secret, in order."""
    found = [secrets.token_urlsafe(32) for _ in identities]
    entries = [
        {"identity": identity, "hash": hashlib.sha256(secret.encode()).hexdigest(), "created": created}
        for identity, secret in zip(identities, found)
    ]
    (root / auth.LEGACY_TOKENS_FILENAME).write_text(json.dumps({"tokens": entries}, indent=2) + "\n")
    return found


def _bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


def _rows(root):
    with open_store(root) as store:
        return store.list_tokens()


def _files(root):
    return sorted(path.name for path in root.iterdir() if path.name.startswith("tokens.json"))


# --- the first start ------------------------------------------------------------


def test_first_start_carries_the_agent_token_and_drops_the_human_ones(root):
    agent, developer, requester = write_legacy(root, "agent", "developer", "requester")

    client = TestClient(create_app(root))

    assert auth.principal_for(root, agent) == ("agent", "agent")
    response = client.get(routes.WHOAMI_PATH, headers=_bearer(agent))
    assert response.status_code == 200
    assert response.json() == {"identity": "agent", "user": "agent", "role": "agent"}
    for human in (developer, requester):
        assert auth.principal_for(root, human) is None
        refused = client.get(routes.WHOAMI_PATH, headers=_bearer(human))
        assert refused.status_code == 401
        assert "My tokens" in refused.json()["detail"] and "user page" in refused.json()["detail"]
        assert refused.headers["WWW-Authenticate"] == "Bearer"
    assert _files(root) == ["tokens.json.migrated"]


def test_the_carried_token_keeps_its_hash_and_date_under_a_legacy_name(root):
    [agent] = write_legacy(root, "agent", created="2026-08-01")

    create_app(root)

    assert _rows(root) == [Token(
        owner="agent", name="legacy-1", hash=auth.hash_token(agent),
        last4=auth.LEGACY_TOKEN_LAST4, created="2026-08-01",
    )]


def test_each_agent_token_gets_its_own_legacy_name(root):
    first, second = write_legacy(root, "agent", "agent")

    create_app(root)

    assert [token.name for token in _rows(root)] == ["legacy-1", "legacy-2"]
    assert auth.principal_for(root, first) == ("agent", "agent")
    assert auth.principal_for(root, second) == ("agent", "agent")


def test_the_migration_logs_one_line_with_both_counts(root, caplog):
    write_legacy(root, "agent", "developer", "requester", "developer")

    with caplog.at_level(logging.INFO, logger=auth.__name__):
        create_app(root)

    lines = [record.getMessage() for record in caplog.records if record.name == auth.__name__]
    assert len(lines) == 1
    assert "1 agent token" in lines[0] and "3 human tokens" in lines[0]


def test_a_request_without_any_token_is_told_where_to_get_one(root):
    response = TestClient(create_app(root)).get(routes.WHOAMI_PATH)

    assert response.status_code == 401
    detail = response.json()["detail"]
    assert "My tokens" in detail and "user page" in detail and "specflo serve user token add" in detail


# --- later starts ---------------------------------------------------------------


def test_a_second_start_changes_nothing(root):
    write_legacy(root, "agent", "developer")
    create_app(root)
    rows, files = _rows(root), _files(root)
    renamed = (root / "tokens.json.migrated").read_text()

    create_app(root)

    assert _rows(root) == rows
    assert _files(root) == files
    assert (root / "tokens.json.migrated").read_text() == renamed


def test_a_root_without_a_tokens_file_is_left_alone(root):
    assert auth.migrate_legacy_tokens(root) is None

    assert _rows(root) == [] and _files(root) == []


def test_an_interrupted_migration_does_not_duplicate_the_token(root):
    # The row went in but the file was not renamed before the stop: the
    # next start skips the hash it already holds and only renames the file.
    [agent] = write_legacy(root, "agent")
    with open_store(root) as store:
        store.add_token(Token(
            owner="agent", name="legacy-1", hash=auth.hash_token(agent),
            last4=auth.LEGACY_TOKEN_LAST4, created="2026-09-06",
        ))

    assert auth.migrate_legacy_tokens(root) == (0, 0)

    assert [token.name for token in _rows(root)] == ["legacy-1"]
    assert _files(root) == ["tokens.json.migrated"]


def test_a_legacy_name_the_agent_already_uses_is_skipped(root):
    auth.mint_user_token(root, auth.AGENT_PRINCIPAL, "legacy-1")
    write_legacy(root, "agent")

    auth.migrate_legacy_tokens(root)

    assert [token.name for token in _rows(root)] == ["legacy-1", "legacy-2"]


def test_an_older_renamed_file_is_kept_and_the_new_one_numbered(root):
    (root / "tokens.json.migrated").write_text("older\n")
    write_legacy(root, "agent")

    auth.migrate_legacy_tokens(root)

    assert (root / "tokens.json.migrated").read_text() == "older\n"
    assert _files(root) == ["tokens.json.migrated", "tokens.json.migrated.1"]


def test_an_unreadable_tokens_file_refuses_to_start_and_is_kept(root):
    (root / auth.LEGACY_TOKENS_FILENAME).write_text("not json")

    with pytest.raises(SpecfloError, match="tokens.json"):
        create_app(root)

    assert _files(root) == ["tokens.json"]


# --- the retired command and the hints ------------------------------------------


def test_serve_token_add_is_an_unknown_command(root):
    result = runner.invoke(app, ["serve", "--root", str(root), "token", "add", "developer"])

    assert result.exit_code != 0
    assert "No such command 'token'" in result.output
    assert not (root / auth.LEGACY_TOKENS_FILENAME).exists()


def test_the_guide_no_longer_lists_serve_token_add():
    names = [command["name"] for command in guide.COMMANDS]

    assert "serve token add" not in names
    assert "serve user token add" in names


def test_a_remote_without_a_token_is_told_to_get_a_personal_one(tmp_path):
    with pytest.raises(SpecfloError) as refused:
        config.add_remote(tmp_path, "team", "http://127.0.0.1:8741", "")

    message = str(refused.value)
    assert "personal token" in message and "My tokens" in message and "user page" in message
    assert "specflo serve user token add" in message
    assert "serve token add" not in message.replace("serve user token add", "")


def test_remote_add_help_names_a_personal_token():
    result = runner.invoke(app, ["remote", "add", "--help"], terminal_width=200)

    assert result.exit_code == 0, result.output
    text = " ".join(word for word in result.output.split() if word != "\u2502")
    assert "personal token" in text and "My tokens" in text and "user page" in text
    assert "specflo serve user token add" in text
