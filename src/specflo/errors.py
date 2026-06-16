"""Shared error types."""


class SpecfloError(Exception):
    """A user-facing error.

    The CLI catches these, prints the message, and exits non-zero — so the
    message should read as guidance to the user, not a stack trace.
    """
