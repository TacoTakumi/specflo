"""What the gateway remembers across restarts: its first start, the events it handled and its cursors.

The gateway acts on no label event older than its first start, and on no
event twice. Both hold across a restart because this store keeps them in the
daemon root, in ``gateway/state.json``: the time the gateway first started,
the newest label event it handled on each issue, and each repo's poll cursor,
the time a poll asks for changes since.

Only the label that is on an issue now can start a run (see
:mod:`specflo.gateway.poll`), so the store keeps one event per issue: an
event on that issue that is not newer than the handled one counts as handled.
The file grows with the number of labelled issues and not with time.

Every write replaces the file whole (a scratch file in the same dir, then
``os.replace``), so a crash or a full disk mid-write leaves the old file. A
file that cannot be read is kept aside as ``state.json.bad`` and the store
starts afresh with the first start now: no event before now is acted on, so
nothing the lost file recorded is handled twice. The file is small and the
gateway writes it a few times a poll, so the I/O is plain and sync.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .poll import Trigger

#: The gateway's dir in the daemon root.
GATEWAY_DIRNAME = "gateway"

#: The store's file in the gateway's dir.
STATE_FILE = "state.json"

#: What a store file that cannot be read is renamed to.
BAD_SUFFIX = ".bad"

_log = logging.getLogger(__name__)


def state_path(root: Path) -> Path:
    """The store file of the daemon root *root*."""
    return Path(root) / GATEWAY_DIRNAME / STATE_FILE


def open_store(root: Path, now: datetime | None = None) -> GatewayStore:
    """The gateway's store in the daemon root *root*.

    The first open records *now* (the time it is called, by default) as the
    gateway's first start; a later open keeps the time recorded. A naive
    *now* is refused.
    """
    now = _aware(datetime.now(UTC) if now is None else now)
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    found = _load(path)
    if found is None:
        found = GatewayStore(path, now)
        found.save()
    return found


@dataclass
class GatewayStore:
    """The store at *path*. ``handled`` maps an issue's key to the time of
    the newest event handled on it, ``cursors`` a repo's key to its cursor."""

    path: Path
    first_start: datetime
    handled: dict[str, datetime] = field(default_factory=dict)
    cursors: dict[str, datetime] = field(default_factory=dict)

    def is_handled(self, trigger: Trigger) -> bool:
        """Whether *trigger*'s issue has a handled event as new as *trigger*'s, or newer."""
        done = self.handled.get(_issue_key(trigger.forge, trigger.repo, trigger.number))
        return done is not None and trigger.at <= done

    def mark_handled(self, trigger: Trigger) -> None:
        """Record *trigger* as handled, in the file before this returns."""
        key = _issue_key(trigger.forge, trigger.repo, trigger.number)
        done = self.handled.get(key)
        if done is not None and trigger.at <= done:
            return
        self.handled[key] = _aware(trigger.at)
        self.save()

    def since(self, forge: str, repo: str) -> datetime | None:
        """The poll cursor of *repo* on *forge*, or None before its first change was seen."""
        return self.cursors.get(_repo_key(forge, repo))

    def set_since(self, forge: str, repo: str, when: datetime) -> None:
        """Move the poll cursor of *repo* on *forge* to *when*, in the file before this returns."""
        key = _repo_key(forge, repo)
        if self.cursors.get(key) == when:
            return
        self.cursors[key] = _aware(when)
        self.save()

    def save(self) -> None:
        """Replace the file with what the store holds now."""
        data = {
            "first_start": _format(self.first_start),
            "handled": {key: _format(at) for key, at in sorted(self.handled.items())},
            "cursors": {key: _format(at) for key, at in sorted(self.cursors.items())},
        }
        _write_atomic(self.path, json.dumps(data, indent=2) + "\n")


def _load(path: Path) -> GatewayStore | None:
    """The store in the file at *path*, or None when there is none to read."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
        return GatewayStore(
            path,
            _parse(data["first_start"]),
            {str(key): _parse(at) for key, at in data.get("handled", {}).items()},
            {str(key): _parse(at) for key, at in data.get("cursors", {}).items()},
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        bad = path.with_name(path.name + BAD_SUFFIX)
        os.replace(path, bad)
        _log.warning(
            "gateway store %s cannot be read; kept as %s and started afresh, "
            "acting on no label event older than now", path, bad,
        )
        return None


def _write_atomic(path: Path, text: str) -> None:
    fd, scratch = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(scratch, path)
    except BaseException:
        Path(scratch).unlink(missing_ok=True)
        raise


def _issue_key(forge: str, repo: str, number: int) -> str:
    return f"{forge}:{repo}#{int(number)}"


def _repo_key(forge: str, repo: str) -> str:
    return f"{forge}:{repo}"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a time the gateway stores must carry its zone.")
    return value


def _format(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("a stored time must be a string.")
    return _aware(datetime.fromisoformat(value)).astimezone(UTC)
