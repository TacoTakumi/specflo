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
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import secrets
from pathlib import Path

from ..errors import SpecfloError

IDENTITIES = ("requester", "developer", "agent")
# The identities a browser can sign in as. The agent has no browser: the
# sign-in page offers only these and refuses an agent token.
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
