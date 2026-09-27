"""Shared error types."""


class SpecfloError(Exception):
    """A user-facing error.

    The CLI catches these, prints the message, and exits non-zero — so the
    message should read as guidance to the user, not a stack trace.
    """


class ProjectNotFound(SpecfloError):
    """No project of that name is held here.

    A ``SpecfloError`` like any other to the CLI and the daemon's API; the
    web pages tell it apart from a project that is held but cannot be read.
    """


def require_one_line(what: str, value: str | None) -> None:
    """Refuse ``value`` when it spans more than one line.

    Artifacts are read back with ``str.splitlines()``, which breaks on far more
    than ``\\n`` and ``\\r``, so a line break in a field would start a line of
    its own: a new entry heading, a field, or a jump in the next ID.
    """
    if value and value.splitlines() != [value]:
        raise SpecfloError(f"{what} is one line: remove the line break.")
