"""The chat log: one durable, append-only transcript per hosted project.

The daemon writes here what a project's conversation produces: a user line
from any seat, an assistant message once its text is whole, a state change
of the agent. Each entry carries an id that only grows, a time, a kind, an
author, and text. The web page follows the log over a stream and replays
from the last id it saw, so the ids are the contract with the browser: a
new entry always has a higher id than every entry before it, across daemon
restarts too.

Storage is one JSON-lines file per project under the daemon root, written
one whole line per entry. A restart reads the last whole line to continue
the ids; a line torn by a crash mid-write is skipped on read and overtaken
by the next id. One process holds one log per project, with a lock, so
two threads appending at once cannot mint one id twice.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import threading
from pathlib import Path

from ..projects import validate_slug

CHAT_DIRNAME = "chat"
# The author a user line names itself with on the wire: a seat, or the
# daemon for the prompts it sends on its own account.
DAEMON_AUTHOR = "daemon"
LABEL_SEPARATOR = ": "
# Every field an entry carries, in the order a line writes them.
ENTRY_FIELDS = ("id", "time", "kind", "author", "text")


@dataclasses.dataclass(frozen=True)
class Entry:
    """One line of a project's transcript."""

    id: int
    time: str
    kind: str
    author: str
    text: str


def label(author: str, text: str) -> str:
    """``text`` as ``author`` sends it: prefixed with the author's label."""
    return f"{author}{LABEL_SEPARATOR}{text}"


def split_label(text: str, default: str, known: tuple[str, ...] = ("requester", "developer", DAEMON_AUTHOR)) -> tuple[str, str]:
    """The author a user line names itself with, and the rest; ``default`` when it names none."""
    for author in known:
        prefix = author + LABEL_SEPARATOR
        if text.startswith(prefix):
            return author, text[len(prefix):]
    return default, text


def log_path(root: Path, slug: str) -> Path:
    """Where the daemon root keeps the transcript for ``slug``."""
    return Path(root) / CHAT_DIRNAME / f"{validate_slug(slug)}.jsonl"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _parse(line: str) -> Entry | None:
    """The entry a line holds, or None for a line that is not a whole entry."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != set(ENTRY_FIELDS) or not isinstance(data["id"], int):
        return None
    return Entry(**{field: data[field] for field in ENTRY_FIELDS})


class ChatLog:
    """A project's transcript: append at the end, read from an id."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._last_id: int | None = None

    def _entries(self) -> list[Entry]:
        if not self.path.is_file():
            return []
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            entry = _parse(line)
            if entry is not None:
                entries.append(entry)
        return entries

    @property
    def last_id(self) -> int:
        """The highest id written; 0 for a log with nothing in it."""
        with self._lock:
            return self._last_id_locked()

    def _last_id_locked(self) -> int:
        if self._last_id is None:
            entries = self._entries()
            self._last_id = entries[-1].id if entries else 0
        return self._last_id

    def append(self, kind: str, author: str, text: str) -> Entry:
        """Write one entry with the next id and return it."""
        with self._lock:
            entry = Entry(id=self._last_id_locked() + 1, time=_now(), kind=kind, author=author, text=text)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as handle:
                # A line torn by a crash has no newline; the new entry starts
                # on a line of its own, so the torn one stays skippable.
                if handle.tell() > 0 and not self._ends_with_newline():
                    handle.write(b"\n")
                handle.write((json.dumps(dataclasses.asdict(entry)) + "\n").encode("utf-8"))
            self._last_id = entry.id
            return entry

    def _ends_with_newline(self) -> bool:
        with open(self.path, "rb") as handle:
            handle.seek(-1, 2)
            return handle.read(1) == b"\n"

    def read_from(self, after_id: int) -> list[Entry]:
        """Every entry with an id above ``after_id``, in order."""
        return [entry for entry in self._entries() if entry.id > after_id]

    def tail(self, after_id: int, offset: int) -> tuple[list[Entry], int]:
        """The entries above ``after_id`` that landed from byte ``offset`` on, and where to read from next.

        A follower keeps the offset between calls so each read costs only
        what landed since, not the whole file. Only whole lines count: a
        line still being written is left for the next call, and the offset
        returned stops before it. A file shorter than the offset was
        replaced; the read starts over from the top.
        """
        if not self.path.is_file():
            return [], 0
        with open(self.path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if offset > size:
                offset = 0
            handle.seek(offset)
            chunk = handle.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return [], offset
        whole = chunk[: end + 1]
        entries = []
        for line in whole.decode("utf-8", errors="replace").splitlines():
            entry = _parse(line)
            if entry is not None and entry.id > after_id:
                entries.append(entry)
        return entries, offset + len(whole)


_open: dict[Path, ChatLog] = {}
_registry = threading.Lock()


def open_log(root: Path, slug: str) -> ChatLog:
    """The one log this process holds for ``slug`` under ``root``."""
    path = log_path(root, slug)
    with _registry:
        log = _open.get(path)
        if log is None:
            log = _open[path] = ChatLog(path)
    return log


def forget_open_logs() -> None:
    """Drop every held log, so the next open reads the file afresh; for tests."""
    with _registry:
        _open.clear()
