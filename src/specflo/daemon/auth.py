"""Bearer tokens bound to the three identities the daemon knows.

The daemon serves three fixed identities: requester, developer, and agent.
The first two are people at a browser or a CLI; agent is the pipeline agent
the daemon drives, which acts over the API only. A token is minted on the
serve side for one of them and shown once; the daemon keeps only the token's
hash, beside the state store under its root. A request resolves its bearer
token to the identity it was minted for, so every route knows who is acting
and a mutation can record it.

Plain files and the standard library only: minting a token needs no web
stack, so it works wherever the daemon root does.

Named users sign in with a password, kept only as a scrypt hash. The hash
string names its own parameters, ``scrypt$N$r$p$<salt>$<hash>`` with salt
and hash in hex, so a hash made under older parameters still verifies and
:func:`needs_rehash` tells the caller to replace it at the next sign-in.
A user is added and changed on the serve side with :func:`add_user` and
the verbs after it; the rows live in the state store, and this module holds
only the rules a user must meet. A browser session carries no role: each
request reads the current user row through :func:`session_user` or
:func:`resume_session`, so a role change applies at the user's next request.
"""

from __future__ import annotations

import datetime
import functools
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path

from ..errors import SpecfloError
from .store import ROLES, Conflict, Session, User, open_store

IDENTITIES = ("requester", "developer", "agent")
# The identities a person at a browser or a CLI acts as; the agent has no
# browser. A browser now signs in as a named user, never with a token.
BROWSER_IDENTITIES = ("requester", "developer")
TOKENS_FILENAME = "tokens.json"

# The scrypt parameters a new password hash is made with: N=2**14, r=8,
# p=5, a 16-byte salt per hash and a 32-byte key.
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 5
SCRYPT_SALT_BYTES = 16
SCRYPT_KEY_BYTES = 32
# The ceiling on what one stored hash may ask scrypt to allocate; a stored
# string past it verifies false instead of exhausting memory.
SCRYPT_MAX_MEMORY = 256 * 1024 * 1024

# How long a browser session lives past its last use, in seconds, and how
# long its row goes between writes: a request within a minute of the last
# write moves nothing, so a page that asks many times a minute writes once.
SESSION_TTL = 12 * 60 * 60
SESSION_WRITE_EVERY = 60

# A user name: 1 to 64 characters from a-z 0-9 . _ - @, starting with a
# letter or digit. Short and plain, so it is safe in a URL, a log line and a
# token listing without quoting.
USER_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._@-]{0,63}")
USER_NAME_RULE = (
    "a user name is 1 to 64 characters from a-z 0-9 . _ - @,"
    " starting with a letter or digit"
)
# Names no user may take: 'agent' is the built-in principal the working
# agents act as, and a user by that name would be mistaken for them.
RESERVED_USER_NAMES = ("agent",)


def tokens_path(root: Path) -> Path:
    """Where the daemon root keeps its token hashes."""
    return Path(root) / TOKENS_FILENAME


def hash_token(secret: str) -> str:
    """The stored form of a token: its SHA-256 hex digest."""
    return hashlib.sha256(secret.encode()).hexdigest()


def validate_identity(identity: str) -> str:
    """``identity``, or a refusal naming the three the daemon knows."""
    if identity not in IDENTITIES:
        raise SpecfloError(
            f"Unknown identity {identity!r}: expected one of " + ", ".join(IDENTITIES) + "."
        )
    return identity


def _entries(root: Path) -> list[dict]:
    path = tokens_path(root)
    if not path.is_file():
        return []
    return json.loads(path.read_text()).get("tokens", [])


def mint_token(root: Path, identity: str, today: str | None = None) -> str:
    """Mint a fresh token for ``identity`` and return the secret, once.

    Only the hash is written, with the identity and the mint date. Every
    token minted before stays valid; there is one file and it is appended to.
    """
    identity = validate_identity(identity)
    secret = secrets.token_urlsafe(32)
    entries = _entries(root)
    entries.append({
        "identity": identity,
        "hash": hash_token(secret),
        "created": today or datetime.date.today().isoformat(),
    })
    path = tokens_path(root)
    path.touch(mode=0o600, exist_ok=True)
    path.write_text(json.dumps({"tokens": entries}, indent=2) + "\n")
    return secret


def identity_for(root: Path, secret: str) -> str | None:
    """The identity ``secret`` was minted for, or None for an unknown token."""
    if not secret:
        return None
    digest = hash_token(secret)
    for entry in _entries(root):
        if secrets.compare_digest(str(entry.get("hash", "")), digest):
            return entry["identity"]
    return None


def validate_user_name(name: str) -> str:
    """``name`` lowercased, or a refusal naming the rule it breaks.

    The check runs on the lowercased name, so 'Rob' is kept as 'rob'.
    """
    lowered = name.lower()
    if not USER_NAME_PATTERN.fullmatch(lowered):
        raise SpecfloError(f"Bad user name {name!r}: {USER_NAME_RULE}.")
    if lowered in RESERVED_USER_NAMES:
        raise SpecfloError(
            f"The user name {lowered!r} is reserved for the built-in principal"
            " of the working agents."
        )
    return lowered


def validate_role(role: str) -> str:
    """``role``, or a refusal naming the roles a user may hold."""
    if role not in ROLES:
        raise SpecfloError(f"Unknown role {role!r}: expected one of " + ", ".join(ROLES) + ".")
    return role


def check_new_user(root: Path, name: str, role: str) -> str:
    """The name a new user would be stored under, or a refusal.

    Refuses a bad name, an unknown role and a name already taken, so a
    caller can check before it asks for a password.
    """
    name = validate_user_name(name)
    validate_role(role)
    with open_store(root) as store:
        if store.get_user(name) is not None:
            raise SpecfloError(f"A user named {name!r} already exists.")
    return name


def add_user(root: Path, name: str, role: str, password: str | None, today: str | None = None) -> User:
    """Add the user ``name`` with ``role``; the user as stored.

    The name is stored lowercase. With ``password`` only its scrypt hash is
    kept; with None the user has no password and is a proxy-only account.
    An empty password is refused, as is anything :func:`check_new_user`
    refuses.
    """
    name = check_new_user(root, name, role)
    if password is not None and not password:
        raise SpecfloError("A password may not be empty.")
    user = User(
        name=name,
        role=role,
        password_hash=hash_password(password) if password is not None else None,
        created=today or datetime.date.today().isoformat(),
    )
    with open_store(root) as store:
        try:
            store.add_user(user)
        except Conflict:
            raise SpecfloError(f"A user named {name!r} already exists.") from None
    return user


def list_users(root: Path) -> list[tuple[User, int]]:
    """Every user in name order, each with the number of tokens they own."""
    with open_store(root) as store:
        return [(user, len(store.list_tokens(user.name))) for user in store.list_users()]


def require_user(root: Path, name: str) -> User:
    """The user called ``name`` (in any case), or a refusal saying there is none."""
    with open_store(root) as store:
        user = store.get_user(name.lower())
    if user is None:
        raise SpecfloError(f"No user named {name.lower()!r}.")
    return user


def set_password(root: Path, name: str, password: str | None) -> User:
    """Replace the password of ``name``, or clear it with None; the updated user.

    Every session of the user ends with it, so a browser signed in under
    the old password, or before the account went proxy-only, must sign in
    again. An empty password and an unknown user are refused.
    """
    if password is not None and not password:
        raise SpecfloError("A password may not be empty.")
    name = require_user(root, name).name
    password_hash = hash_password(password) if password is not None else None
    with open_store(root) as store:
        # The password first, then the sessions: a session opened under the
        # old password just before the change is still among those dropped.
        user = store.set_user_password(name, password_hash)
        store.remove_user_sessions(name)
    if user is None:
        raise SpecfloError(f"No user named {name!r}.")
    return user


def set_role(root: Path, name: str, role: str) -> User:
    """Replace the role of ``name``; the updated user. Unknown user or role is refused."""
    validate_role(role)
    name = require_user(root, name).name
    with open_store(root) as store:
        user = store.set_user_role(name, role)
    if user is None:
        raise SpecfloError(f"No user named {name!r}.")
    return user


def remove_user(root: Path, name: str) -> str:
    """Remove ``name`` with their tokens and sessions; the name removed.

    The store drops all three in one transaction. An unknown user is refused.
    """
    name = name.lower()
    with open_store(root) as store:
        removed = store.remove_user(name)
    if not removed:
        raise SpecfloError(f"No user named {name!r}.")
    return name


def session_user(root: Path, secret: str, now: float | None = None) -> User | None:
    """The user a browser session cookie stands for, read live, or None.

    The session is found by the SHA-256 of ``secret``. None for an empty or
    unknown secret, or a session whose expiry is at or before ``now``.
    Otherwise the user's row as it is now, so a role changed since the
    session opened is the role this request acts under.
    """
    if not secret:
        return None
    now = time.time() if now is None else now
    with open_store(root) as store:
        session = store.get_session(hash_token(secret))
        if session is None or session.expires <= now:
            return None
        return store.get_user(session.user)


def check_password(root: Path, name: str, password: str) -> User | None:
    """The user ``name`` (in any case) when ``password`` is theirs, else None.

    An unknown name, a proxy-only user and a wrong password all answer None,
    and each costs one scrypt run, so neither the answer nor how long it
    takes tells which. A right password over a hash made under older
    parameters replaces that hash with one under today's.
    """
    with open_store(root) as store:
        user = store.get_user(name.lower())
    if user is None or user.password_hash is None:
        verify_password(password, _decoy_hash())
        return None
    if not verify_password(password, user.password_hash):
        return None
    if needs_rehash(user.password_hash):
        with open_store(root) as store:
            user = store.set_user_password(user.name, hash_password(password)) or user
    return user


@functools.cache
def _decoy_hash() -> str:
    # What a sign-in with no stored hash checks against, so it takes as long
    # as one with a hash; no password is ever its own.
    return hash_password(secrets.token_urlsafe(32))


def open_session(root: Path, user: str, method: str, now: float) -> str:
    """Start a browser session for ``user`` and return its cookie secret, once.

    The store keeps only the secret's hash, with the user, how the session
    was opened, and an expiry :data:`SESSION_TTL` after ``now``. Sessions
    already expired are dropped on the way.
    """
    secret = secrets.token_urlsafe(32)
    with open_store(root) as store:
        store.remove_expired_sessions(now)
        store.add_session(Session(
            hash=hash_token(secret), user=user, method=method,
            expires=now + SESSION_TTL, written=now,
        ))
    return secret


def resume_session(root: Path, secret: str, now: float) -> tuple[User, bool] | None:
    """The user a live session stands for, read live, and whether its row was written.

    None for an empty or unknown secret, a session whose user is gone, or
    one whose expiry is at or before ``now``; an expired session is dropped
    on sight. A live session's expiry moves to :data:`SESSION_TTL` after
    ``now``, but only once :data:`SESSION_WRITE_EVERY` has passed since its
    row was last written; before that the row is left as it is.
    """
    if not secret:
        return None
    digest = hash_token(secret)
    with open_store(root) as store:
        session = store.get_session(digest)
        if session is None:
            return None
        if session.expires <= now:
            store.remove_session(digest)
            return None
        user = store.get_user(session.user)
        if user is None:
            return None
        written = now - session.written >= SESSION_WRITE_EVERY
        if written:
            store.update_session(digest, expires=now + SESSION_TTL, written=now)
    return user, written


def hash_password(password: str) -> str:
    """The stored form of ``password``: a scrypt hash under a fresh random salt."""
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_KEY_BYTES)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Whether ``password`` hashes to ``stored``, under the parameters ``stored`` names.

    A malformed stored string, or one asking for more memory than
    :data:`SCRYPT_MAX_MEMORY`, verifies false rather than raising.
    """
    parsed = _parse(stored)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        digest = _scrypt(password, salt, n, r, p, len(expected))
    except (ValueError, OverflowError, MemoryError):
        return False
    return hmac.compare_digest(digest, expected)


def needs_rehash(stored: str) -> bool:
    """Whether ``stored`` was made under other parameters than today's, or is unreadable."""
    parsed = _parse(stored)
    if parsed is None:
        return True
    n, r, p, _salt, expected = parsed
    return (n, r, p, len(expected)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_KEY_BYTES)


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    # scrypt needs about 128 * r * (N + p) bytes; past the ceiling, refuse
    # before asking the library for it.
    needed = 128 * r * (n + p + 2)
    if needed > SCRYPT_MAX_MEMORY:
        raise ValueError("scrypt parameters ask for too much memory")
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=needed + 1024 * 1024
    )


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    """The parameters, salt and hash a stored string names, or None if it is malformed."""
    parts = stored.split("$") if isinstance(stored, str) else []
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = (int(part) for part in parts[1:4])
        salt, digest = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        return None
    if n < 2 or n & (n - 1) or r < 1 or p < 1 or not salt or not digest:
        return None
    return n, r, p, salt, digest
