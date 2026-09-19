"""Egress ceilings: how open a member a request may be served by.

A member's egress class says where what it is sent goes, and the classes run
from the strictest to the most open (see ``definitions``). A request has a
ceiling, one class, and only a member whose class is not more open than it
may serve the request.

The ceiling is the strictest of a list of classes: the one the request names,
and every limit that stands over the request whatever it names - the class
the pool's definition accepts is one, and the class the requesting project
pins on its record is another. A request that names no class names the
default, so the most open class is never reached by saying nothing. A limit
more can only make the ceiling stricter; nothing a request says widens one.

Everything here is a function of the classes it is handed. It reads no
configuration and no lease: the pool service works the ceiling out, and the
ledger is handed the classes within it.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..errors import SpecfloError
from .definitions import DEFAULT_EGRESS, EGRESS_CLASSES


class UnknownClass(SpecfloError):
    """A name that is not an egress class; the message names those that are."""


def strictest(classes: Iterable[str]) -> str:
    """The strictest of *classes*, of which there is at least one."""
    return EGRESS_CLASSES[min(_rank(name) for name in classes)]


def ceiling(asked: str | None, limits: Iterable[str]) -> str:
    """The ceiling of a request that names the class *asked*, or none, under *limits*."""
    return strictest([DEFAULT_EGRESS if asked is None else asked, *limits])


def allowed(member_class: str, limit: str) -> bool:
    """May a member of *member_class* serve a request whose ceiling is *limit*?"""
    return _rank(member_class) <= _rank(limit)


def within(limit: str) -> tuple[str, ...]:
    """The classes not more open than *limit*, strictest first."""
    return EGRESS_CLASSES[: _rank(limit) + 1]


def _rank(name: str) -> int:
    """Where *name* stands among the classes; the strictest is 0."""
    if name not in EGRESS_CLASSES:
        raise UnknownClass(
            f"'{name}' is not an egress class; the classes, strictest first: "
            + ", ".join(EGRESS_CLASSES) + "."
        )
    return EGRESS_CLASSES.index(name)
