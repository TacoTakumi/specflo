"""The operator's register of checkouts that hold tokens.

A checkout keeps the lease tokens its holder was granted in
``.specflo/leases`` and the bearer token of each daemon it reaches in
``.specflo/remotes``. The pool's sandbox hides the operator's home, and a
path a definition lists brings back what it covers, tokens included. So the
pool hides the token directories of every checkout below a listed path, and
it learns where those checkouts are from this register rather than by walking
the listed path, which on a data directory takes seconds at every start.

A checkout is recorded by the command that makes one of its token
directories, and by any command run inside a checkout that already has one.
A checkout whose tokens were written by nothing that records, and in which no
command has run since, is not known to the pool.

The register lives in the home's ``.specflo`` directory, which the sandbox
hides from every member, and holds one real path per line. Recording is a
convenience to the command that does it, never a reason for it to fail.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from .config import CONFIG_DIRNAME, REMOTES_DIRNAME, find_root

REGISTER_FILENAME = "checkouts"

# The directory a checkout keeps its lease tokens in, under its .specflo. The
# agent subsystem's lease module and the pool's launch module hold copies.
_LEASES_DIRNAME = "leases"


def register_file(home: Path | str | None = None) -> Path:
    """Where the register is, under *home* or this user's home."""
    return Path(home or Path.home()) / CONFIG_DIRNAME / REGISTER_FILENAME


def token_dirs(root: Path | str) -> tuple[Path, Path]:
    """The two directories of tokens a checkout at *root* keeps."""
    folder = Path(root) / CONFIG_DIRNAME
    return folder / _LEASES_DIRNAME, folder / REMOTES_DIRNAME


def recorded(home: Path | str | None = None) -> tuple[str, ...]:
    """Every checkout in the register, by real path, each once."""
    try:
        text = register_file(home).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    return tuple(dict.fromkeys(line for line in text.splitlines() if os.path.isabs(line)))


def record(root: Path | str, home: Path | str | None = None) -> None:
    """Add the checkout at *root* to the register, by its real path."""
    path = os.path.realpath(root)
    if "\n" in path or path in recorded(home):
        return
    file = register_file(home)
    with contextlib.suppress(OSError):
        file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(path + "\n")


def record_if_held(start: Path | str) -> None:
    """Record the checkout *start* is in when it has a token directory."""
    with contextlib.suppress(OSError):
        root = find_root(Path(start))
        if root is not None and any(held.is_dir() for held in token_dirs(root)):
            record(root)
