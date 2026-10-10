"""Named users, their tokens and their sign-in sessions in the state store.

The daemon moves from three fixed identities to named users. A user, every
token minted for a user (or for the built-in agent, which has no user row)
and every browser session live in the state store and are read and written
through the :class:`Store` interface only. Passwords are kept as scrypt
hashes whose string carries its own parameters, so a hash made with older
parameters still verifies and can be told apart for a rehash.
"""

import hashlib
import inspect
import re
from pathlib import Path

import pytest

import specflo
from specflo.daemon import auth
from specflo.daemon import store as store_module
from specflo.daemon.store import Conflict, Session, Token, User


@pytest.fixture
def store(tmp_path):
    with store_module.SqliteStore(tmp_path / "state.db") as store:
        yield store


def _user(name="ada", role="developer", password_hash="scrypt$x"):
    return User(name=name, role=role, password_hash=password_hash, created="2026-10-10")


def _token(owner="ada", name="laptop", secret="secret-ada-laptop"):
    return Token(
        owner=owner,
        name=name,
        hash=hashlib.sha256(secret.encode()).hexdigest(),
        last4=secret[-4:],
        created="2026-10-10",
        last_used=None,
    )


def _session(hash_="s1", user="ada", method="password", expires=2000.0, written=1000.0):
    return Session(hash=hash_, user=user, method=method, expires=expires, written=written)


# --- users ------------------------------------------------------------------


def test_a_user_reads_back_as_added(store):
    store.add_user(_user())

    assert store.get_user("ada") == _user()
    assert store.get_user("bob") is None


def test_a_proxy_only_user_has_no_password_hash(store):
    store.add_user(_user("bob", role="requester", password_hash=None))

    assert store.get_user("bob").password_hash is None


def test_users_list_in_name_order(store):
    store.add_user(_user("carol", role="requester"))
    store.add_user(_user("ada"))

    assert [user.name for user in store.list_users()] == ["ada", "carol"]


def test_a_taken_user_name_is_a_conflict(store):
    store.add_user(_user())

    with pytest.raises(Conflict):
        store.add_user(_user(role="requester"))


def test_a_user_name_must_be_lowercase(store):
    with pytest.raises(ValueError):
        store.add_user(_user("Ada"))


def test_a_role_outside_the_two_is_refused(store):
    with pytest.raises(ValueError):
        store.add_user(_user(role="admin"))


def test_the_password_hash_is_replaced_or_cleared(store):
    store.add_user(_user())

    assert store.set_user_password("ada", "scrypt$new").password_hash == "scrypt$new"
    assert store.set_user_password("ada", None).password_hash is None
    assert store.set_user_password("bob", "scrypt$x") is None


def test_the_role_is_replaced(store):
    store.add_user(_user())

    assert store.set_user_role("ada", "requester").role == "requester"
    assert store.get_user("ada").role == "requester"
    assert store.set_user_role("bob", "developer") is None
    with pytest.raises(ValueError):
        store.set_user_role("ada", "admin")


def test_removing_a_user_drops_their_tokens_and_sessions_only(store):
    store.add_user(_user("ada"))
    store.add_user(_user("bob", role="requester"))
    store.add_token(_token("ada", "laptop", "secret-ada-laptop"))
    store.add_token(_token("bob", "phone", "secret-bob-phone"))
    store.add_token(_token("agent", "pipeline", "secret-agent"))
    store.add_session(_session("s-ada", "ada"))
    store.add_session(_session("s-bob", "bob"))

    assert store.remove_user("ada") is True

    assert store.get_user("ada") is None
    assert store.list_tokens("ada") == []
    assert store.get_session("s-ada") is None
    assert store.get_user("bob") is not None
    assert [token.name for token in store.list_tokens("bob")] == ["phone"]
    assert [token.name for token in store.list_tokens("agent")] == ["pipeline"]
    assert store.get_session("s-bob") is not None


def test_removing_an_unknown_user_reports_false(store):
    assert store.remove_user("nobody") is False


def test_removing_a_user_is_one_transaction(store, monkeypatch):
    store.add_user(_user())
    store.add_token(_token())
    store.add_session(_session())
    # A failure on the last statement of the removal must leave every row
    # in place: the tokens and sessions deleted before it come back.
    store.connection.execute(
        "CREATE TRIGGER refuse_user_delete BEFORE DELETE ON users"
        " BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )

    with pytest.raises(Exception):
        store.remove_user("ada")

    assert store.get_user("ada") is not None
    assert [token.name for token in store.list_tokens("ada")] == ["laptop"]
    assert store.get_session("s1") is not None


# --- tokens -----------------------------------------------------------------


def test_a_token_reads_back_by_hash_and_by_owner(store):
    token = _token()
    store.add_token(token)

    assert store.find_token(token.hash) == token
    assert store.find_token("0" * 64) is None
    assert store.list_tokens("ada") == [token]
    assert store.list_tokens("bob") == []


def test_tokens_list_in_name_order_for_one_owner_or_all(store):
    store.add_token(_token("ada", "phone", "secret-1"))
    store.add_token(_token("ada", "laptop", "secret-2"))
    store.add_token(_token("agent", "pipeline", "secret-3"))

    assert [token.name for token in store.list_tokens("ada")] == ["laptop", "phone"]
    assert [(token.owner, token.name) for token in store.list_tokens()] == [
        ("ada", "laptop"), ("ada", "phone"), ("agent", "pipeline"),
    ]


def test_the_agent_owns_tokens_without_a_user_row(store):
    store.add_token(_token("agent", "pipeline", "secret-agent"))

    assert store.get_user("agent") is None
    assert store.list_tokens("agent")[0].owner == "agent"


def test_a_token_name_is_unique_per_owner(store):
    store.add_token(_token("ada", "laptop", "secret-1"))
    store.add_token(_token("bob", "laptop", "secret-2"))

    with pytest.raises(Conflict):
        store.add_token(_token("ada", "laptop", "secret-3"))


def test_a_token_hash_is_unique(store):
    store.add_token(_token("ada", "laptop", "secret-1"))

    with pytest.raises(Conflict):
        store.add_token(_token("bob", "phone", "secret-1"))


def test_revoking_a_token_by_owner_and_name(store):
    store.add_token(_token("ada", "laptop", "secret-1"))
    store.add_token(_token("bob", "laptop", "secret-2"))

    assert store.revoke_token("ada", "laptop") is True
    assert store.revoke_token("ada", "laptop") is False

    assert store.list_tokens("ada") == []
    assert len(store.list_tokens("bob")) == 1


def test_touching_a_token_records_its_last_use(store):
    token = _token()
    store.add_token(token)

    assert store.touch_token(token.hash, "2026-10-11T09:00:00") is True
    assert store.find_token(token.hash).last_used == "2026-10-11T09:00:00"
    assert store.touch_token("0" * 64, "2026-10-11T09:00:00") is False


# --- sessions ---------------------------------------------------------------


def test_a_session_reads_back_by_hash(store):
    store.add_user(_user())
    store.add_session(_session(expires=2000.5, written=1000.25))

    assert store.get_session("s1") == _session(expires=2000.5, written=1000.25)
    assert store.get_session("s2") is None


def test_a_session_hash_is_unique(store):
    store.add_user(_user())
    store.add_session(_session())

    with pytest.raises(Conflict):
        store.add_session(_session())


def test_a_method_outside_the_two_is_refused(store):
    store.add_user(_user())

    with pytest.raises(ValueError):
        store.add_session(_session(method="magic"))


def test_updating_a_session_moves_its_expiry_and_write_time(store):
    store.add_user(_user())
    store.add_session(_session())

    updated = store.update_session("s1", expires=5000.0, written=3000.0)

    assert updated == _session(expires=5000.0, written=3000.0)
    assert store.get_session("s1") == updated
    assert store.update_session("s2", expires=1.0, written=1.0) is None


def test_removing_one_session(store):
    store.add_user(_user())
    store.add_session(_session())

    assert store.remove_session("s1") is True
    assert store.remove_session("s1") is False
    assert store.get_session("s1") is None


def test_removing_a_users_sessions_keeps_other_users(store):
    store.add_user(_user("ada"))
    store.add_user(_user("bob", role="requester"))
    store.add_session(_session("a1", "ada"))
    store.add_session(_session("a2", "ada"))
    store.add_session(_session("b1", "bob"))

    assert store.remove_user_sessions("ada") == 2

    assert store.get_session("a1") is None
    assert store.get_session("a2") is None
    assert store.get_session("b1") is not None


def test_removing_a_users_sessions_but_one(store):
    store.add_user(_user())
    for hash_ in ("a1", "a2", "a3"):
        store.add_session(_session(hash_))

    assert store.remove_user_sessions("ada", keep="a2") == 2

    assert store.get_session("a1") is None
    assert store.get_session("a2") is not None
    assert store.get_session("a3") is None


def test_removing_expired_sessions(store):
    store.add_user(_user())
    store.add_session(_session("old", expires=100.0))
    store.add_session(_session("edge", expires=500.0))
    store.add_session(_session("new", expires=900.0))

    assert store.remove_expired_sessions(500.0) == 2

    assert store.get_session("old") is None
    assert store.get_session("edge") is None
    assert store.get_session("new") is not None


def test_the_tables_survive_a_reopen(tmp_path):
    path = tmp_path / "state.db"
    with store_module.SqliteStore(path) as store:
        store.add_user(_user())
        store.add_token(_token())
        store.add_session(_session())

    with store_module.SqliteStore(path) as store:
        assert store.get_user("ada") == _user()
        assert store.list_tokens("ada") == [_token()]
        assert store.get_session("s1") == _session()


# --- password hashing -------------------------------------------------------


def test_a_password_hash_carries_its_scrypt_parameters():
    stored = auth.hash_password("correct horse")

    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", "16384", "8", "5")
    assert len(bytes.fromhex(salt)) == 16
    assert len(bytes.fromhex(digest)) in (32, 64)
    assert "correct horse" not in stored


def test_the_same_password_gets_a_fresh_salt():
    first = auth.hash_password("correct horse")
    second = auth.hash_password("correct horse")

    assert first.split("$")[4] != second.split("$")[4]
    assert first != second


def test_verify_accepts_the_password_and_refuses_another():
    stored = auth.hash_password("correct horse")

    assert auth.verify_password("correct horse", stored) is True
    assert auth.verify_password("wrong horse", stored) is False


def _older_hash(password, n=8192, r=8, p=5):
    salt = bytes(range(16))
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def test_an_older_parameter_hash_still_verifies_and_needs_a_rehash():
    stored = _older_hash("correct horse")

    assert auth.verify_password("correct horse", stored) is True
    assert auth.verify_password("wrong horse", stored) is False
    assert auth.needs_rehash(stored) is True
    assert auth.needs_rehash(auth.hash_password("correct horse")) is False


@pytest.mark.parametrize("stored", [
    "",
    "plain",
    "bcrypt$16384$8$5$00$00",
    "scrypt$16384$8$5$zz$zz",
    "scrypt$abc$8$5$00$00",
    "scrypt$16383$8$5$00112233$00112233",
    "scrypt$16384$8$5$0011",
    "scrypt$" + str(2**40) + "$8$5$00112233$00112233",
])
def test_a_malformed_stored_hash_verifies_false_without_raising(stored):
    assert auth.verify_password("anything", stored) is False
    assert auth.needs_rehash(stored) is True


def test_verify_compares_in_constant_time():
    assert "hmac.compare_digest" in inspect.getsource(auth.verify_password)


# --- one way in -------------------------------------------------------------

# SQL in this codebase writes its keywords in capitals; a keyword followed by
# one of the three table names is SQL on that table. The lookahead keeps a
# file name such as tokens.json from counting.
_SQL_ON_AUTH_TABLES = re.compile(
    r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE(?:\s+IF\s+(?:NOT\s+)?EXISTS)?)"
    r"\s+[\"'`\[]?(?:users|tokens|sessions)\b(?![\w.])"
)


def test_the_sql_scan_finds_sql_and_skips_prose():
    assert _SQL_ON_AUTH_TABLES.search("SELECT name FROM users WHERE name = ?")
    assert _SQL_ON_AUTH_TABLES.search("CREATE TABLE IF NOT EXISTS sessions (")
    assert _SQL_ON_AUTH_TABLES.search('DELETE FROM "tokens" WHERE owner = ?')
    assert not _SQL_ON_AUTH_TABLES.search("the hashes from tokens.json")
    assert not _SQL_ON_AUTH_TABLES.search("read the identity from users at a browser")


def test_no_sql_on_the_auth_tables_outside_the_store():
    package = Path(specflo.__file__).parent
    backend = package / "daemon" / "store.py"
    offenders = [
        str(path.relative_to(package))
        for path in sorted(package.rglob("*.py"))
        if path != backend and _SQL_ON_AUTH_TABLES.search(path.read_text())
    ]

    assert offenders == []
    assert _SQL_ON_AUTH_TABLES.search(backend.read_text())
