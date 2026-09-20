"""The holder's side of a lease: finding its token, and why a lease ended.

A pooled member's host lets only the lease holder drive it. The ``specflo
agent`` verbs find the holder's token in one of three places, first hit wins:

    --lease-token                       the verb's own option
    $SPECFLO_LEASE_TOKEN                the environment
    .specflo/leases/<agent>.token       found upward from the working directory

When a lease ends the pool stops the member's host and leaves one record in
the agent's state directory, beside status.json:

    <base>/<name>/lease-ended.json
        {"cause": "released" | "expired" | "preempted",
         "request_id": <the preempting request's id, else null>,
         "ended_at": <UTC timestamp>}

so the former holder's next verb can name the cause instead of reporting a
host that is merely unreachable. The pool writes the record (``write_ended``)
and clears it when it starts the member for a new lease (``clear_ended``).

A member that is leased again has a host again, and its wall tells a former
holder only that someone else holds the lease. So the pool, which knows a
holder by the SHA-256 of its token, also keeps the same record for that holder:

    <base>/<name>/lease-ended/<the token's hash>.json

A new lease leaves these where they are, and a verb the wall turns away reads
the one under the hash of the token it presented (``read_ended`` with a
token): a former holder learns how its own lease ended, and any other token
finds nothing. The newest ``ENDED_KEPT`` are kept for one agent; an older one
is removed, and its holder then gets the wall's refusal like anyone else.

A lease on a developer's console ends with the holder's record alone
(``write_ended`` with ``last=False``): the host is the developer's and runs
on, and a last ending left beside it would tell the developer's own verbs,
which carry no token, that a lease of theirs had ended.

Stdlib only, and nothing from the rest of the agent subsystem: this module
knows files and strings, not the host's socket and not pi.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ENV_LEASE_TOKEN = "SPECFLO_LEASE_TOKEN"

#: Where the lease verb stores a token, relative to the client root.
TOKEN_DIR = Path(".specflo") / "leases"

ENDED_FILE = "lease-ended.json"

ENDED_CAUSES = ("released", "expired", "preempted")

#: The former holders' records, one file each, named by the token's hash.
ENDED_DIR = "lease-ended"

#: How many former holders' records are kept for one agent.
ENDED_KEPT = 16

_TOKEN_HASH = re.compile(r"[0-9a-f]{64}")

# How the host words its refusal of a frame the wall turned away.
_WALL_REFUSAL = "lease held"


# -- the token --------------------------------------------------------------


def token_file(root: Path | str, agent: str) -> Path:
    """The token file for one agent under a client root."""
    return Path(root) / TOKEN_DIR / f"{agent}.token"


def find_token(
    agent: str,
    option: str | None = None,
    environ: Mapping[str, str] | None = None,
    start: Path | str | None = None,
) -> str | None:
    """The lease token for this agent, None when there is none to present."""
    if option:
        return option
    environ = os.environ if environ is None else environ
    if environ.get(ENV_LEASE_TOKEN):
        return environ[ENV_LEASE_TOKEN]
    if not agent or Path(agent).name != agent:
        return None  # a name with a path in it names no token file
    here = Path(start) if start is not None else Path.cwd()
    for directory in (here, *here.parents):
        candidate = token_file(directory, agent)
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8").strip() or None
            except OSError:
                return None
    return None


def token_hash(token: str) -> str:
    """What the pool keeps of a lease token, and knows its holder by: its
    SHA-256, in hex."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_wall_refusal(error: Any) -> bool:
    """Did the host refuse a frame because a lease is held by someone else?"""
    return isinstance(error, str) and error.startswith(_WALL_REFUSAL)


# -- the ended record -------------------------------------------------------


def write_ended(
    state_dir: Path | str,
    cause: str,
    request_id: str | None = None,
    ended_at: str | None = None,
    holder: str | None = None,
    *,
    last: bool = True,
) -> Path | None:
    """Record why the lease on this agent ended; atomic, replaces any earlier.

    *holder* is the hash of the lease's token (``token_hash``). With one the
    record is also kept for that holder, past the agent's next lease.

    With *last* False only the holder's record is written, and the agent's
    last ending is left as it was: a console's host is the developer's own
    and runs on, so an ending of the pool's is no ending of theirs to read.
    The path written, None when there was only a *holder* to write for.
    """
    if cause not in ENDED_CAUSES:
        raise ValueError(f"invalid lease end cause {cause!r}: one of {ENDED_CAUSES}")
    if cause == "preempted" and not request_id:
        raise ValueError("a preempted lease records the preempting request's id")
    if holder is not None and not _TOKEN_HASH.fullmatch(holder):
        raise ValueError("a holder is named by the SHA-256 of its lease token, in hex")
    if ended_at is None:
        ended_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    record = {"cause": cause, "request_id": request_id, "ended_at": ended_at}
    if holder is not None:
        _write_record(Path(state_dir) / ENDED_DIR / f"{holder}.json", record)
        _drop_oldest(Path(state_dir) / ENDED_DIR)
    if not last:
        return None
    return _write_record(Path(state_dir) / ENDED_FILE, record)


def _write_record(path: Path, record: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def _drop_oldest(directory: Path) -> None:
    """Keep the newest ``ENDED_KEPT`` holders' records; one that cannot be
    read counts as the oldest."""
    ended_at = {
        path: str((_read_record(path) or {}).get("ended_at") or "")
        for path in directory.glob("*.json")
    }
    for path in sorted(ended_at, key=lambda p: (ended_at[p], p.name))[:-ENDED_KEPT]:
        path.unlink(missing_ok=True)


def read_ended(state_dir: Path | str, token: str | None = None) -> dict[str, Any] | None:
    """The ended record, None when absent or not one this module can report.

    Without a *token* it is the agent's last ending. With one it is the record
    kept for the holder of that token, and None for any other token.
    """
    if token is None:
        return _read_record(Path(state_dir) / ENDED_FILE)
    return _read_record(Path(state_dir) / ENDED_DIR / f"{token_hash(token)}.json")


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("cause") not in ENDED_CAUSES:
        return None
    return record


def clear_ended(state_dir: Path | str) -> None:
    """Forget the last ending: the member is starting under a new lease. What
    is kept for the former holders stays."""
    (Path(state_dir) / ENDED_FILE).unlink(missing_ok=True)


def ended_message(record: Mapping[str, Any]) -> str:
    """The cause as the former holder reads it."""
    cause = record["cause"]
    if cause == "preempted":
        return f"lease preempted by {record.get('request_id')}"
    return f"lease {cause}"
