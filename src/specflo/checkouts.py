"""The operator's register of checkouts that hold tokens.

A checkout keeps the lease tokens its holder was granted in
``.specflo/leases`` and the bearer token of each daemon it reaches in
``.specflo/remotes``. The pool's sandbox hides the operator's home, and a
path a definition lists brings back what it covers, tokens included. So the
pool hides the token directories of every checkout below a listed path, and
it learns where those checkouts are from this register rather than by walking
the listed path, which on a data directory takes seconds at every start.

A checkout is recorded when ``init`` makes it, by the command that makes one
of its token directories, and by any command run inside it. At each start the
pool makes the token directories of every recorded checkout below a listed
path and hides them, so a token written there during the lease stays hidden
too. A checkout that nothing has recorded before a member starts is not known
to that member's sandbox.

The register lives in the home's ``.specflo`` directory, which the sandbox
hides from every member, and holds one real path per line. Recording is a
convenience to the command that does it, never a reason for it to fail.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from pathlib import Path

from .config import CONFIG_DIRNAME, find_root

REGISTER_FILENAME = "checkouts"

# The variable that puts the register somewhere else than the home. The test
# suite sets it, so the specflo commands its tests start as subprocesses keep
# off the operator's register too. The commands and the pool both honour it.
# A register outside the home's .specflo is not hidden from members; it holds
# checkout paths, never a token.
REGISTER_ENV = "SPECFLO_CHECKOUT_REGISTER"


def register_file(
    home: Path | str | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """Where the register is: under *home* when one is given, else the file
    *environ* (this process's environment by default) names, else under the
    home it names."""
    if home is None:
        environ = os.environ if environ is None else environ
        if environ.get(REGISTER_ENV):
            return Path(environ[REGISTER_ENV])
        home = environ.get("HOME") or Path.home()
    return Path(home) / CONFIG_DIRNAME / REGISTER_FILENAME


def recorded(
    home: Path | str | None = None, environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Every checkout in the register, by real path, each once."""
    try:
        text = register_file(home, environ).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    return tuple(dict.fromkeys(line for line in text.splitlines() if os.path.isabs(line)))


def record(
    root: Path | str, home: Path | str | None = None, environ: Mapping[str, str] | None = None
) -> None:
    """Add the checkout at *root* to the register, by its real path."""
    path = os.path.realpath(root)
    if "\n" in path or path in recorded(home, environ):
        return
    file = register_file(home, environ)
    with contextlib.suppress(OSError):
        file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(path + "\n")


def record_found(start: Path | str) -> None:
    """Record the checkout *start* is in, if it is in one."""
    with contextlib.suppress(OSError):
        root = find_root(Path(start))
        if root is not None:
            record(root)
