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
"""

from __future__ import annotations

import datetime
import hashlib
import json
import secrets
from pathlib import Path

from ..errors import SpecfloError

IDENTITIES = ("requester", "developer", "agent")
# The identities a browser can sign in as. The agent has no browser: the
# sign-in page offers only these and refuses an agent token.
BROWSER_IDENTITIES = ("requester", "developer")
TOKENS_FILENAME = "tokens.json"


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
