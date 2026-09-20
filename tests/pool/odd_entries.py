"""Entries of a pool directory that are not regular files, and a guard against a hang.

A named pipe that no process writes to holds whoever opens it for reading for
ever. So a test of such an entry runs what reads the directory in a thread of
its own, and fails when the thread still runs after the limit.
"""

from __future__ import annotations

import io
import os
import threading
from pathlib import Path

# The entries a pool directory may hold under the name of a markdown file
# without one being there: a named pipe, and a folder.
KINDS: tuple[str, ...] = ("pipe", "folder")

# How long a read of a pool directory may take before it counts as a hang.
LIMIT = 10.0


def make_odd(path: Path, kind: str) -> Path:
    """Put an entry of *kind* that is not a regular file at *path*."""
    if kind == "pipe":
        os.mkfifo(path)
    else:
        path.mkdir()
    return path


def _let_go(pipe: Path) -> None:
    """Let a reader that is held on the named *pipe* go: it reads an empty file."""
    try:
        os.close(os.open(pipe, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass  # No reader is held on it, or it is no pipe.


def within(call, *args, pipes: tuple[Path, ...] | list[Path] = (), limit: float = LIMIT):
    """What ``call(*args)`` gives, run in a thread of its own.

    A call that still runs after *limit* seconds fails the test. Each of
    *pipes* is opened for writing first, so that a reader held on one goes on
    and the thread ends before the test does.
    """
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["value"] = call(*args)
        except BaseException as exc:  # Raised again below, in the test's thread.
            outcome["raised"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(limit)
    hung = thread.is_alive()
    # The reader may be held on one pipe after another, so each is let go
    # until the thread ends.
    for _ in range(100):
        if not thread.is_alive():
            break
        for pipe in pipes:
            _let_go(pipe)
        thread.join(0.1)
    assert not hung, f"{getattr(call, '__name__', call)} still ran after {limit} seconds"
    if "raised" in outcome:
        raise outcome["raised"]
    return outcome["value"]


def opened(monkeypatch) -> list[str]:
    """The path of every file that is opened from here on, as it is opened."""
    paths: list[str] = []
    real = io.open

    def spy(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            paths.append(os.fspath(file))
        return real(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", spy)
    return paths
