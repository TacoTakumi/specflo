"""The wire schema: one request and one response shape per operation.

Every ProjectService operation crosses the wire the same way: a POST to the
operation's route carrying its arguments as a JSON object, answered by the
result encoded as JSON. What each operation takes and returns is read from
the protocol itself, so the schema cannot drift from the facade: the daemon
decodes a request by the operation's parameters, and a client decodes the
response by its return type, rebuilding the same values the local service
hands back in-process.

Encoding is structural: a dataclass becomes an object of its fields, a path
a string, a tuple a list. Decoding is driven by the type hint, which is what
turns the list back into a tuple and the object back into the dataclass.
"""

from __future__ import annotations

import dataclasses
import inspect
import types
import typing
from pathlib import Path

from .protocol import ProjectService

ROUTE_PREFIX = "/api"


class WireError(ValueError):
    """A request that does not fit its operation's parameters."""


@dataclasses.dataclass(frozen=True)
class Operation:
    """One protocol operation as it crosses the wire."""

    name: str
    parameters: tuple[inspect.Parameter, ...]
    hints: dict[str, object]
    returns: object

    @property
    def slug_scoped(self) -> bool:
        """Whether the operation addresses one project, named by ``slug``."""
        return bool(self.parameters) and self.parameters[0].name == "slug"

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.parameters if p.default is inspect.Parameter.empty)


def _operations() -> dict[str, Operation]:
    operations = {}
    for name, member in vars(ProjectService).items():
        if name.startswith("_") or not callable(member):
            continue
        parameters = tuple(
            p for p in inspect.signature(member).parameters.values() if p.name != "self"
        )
        hints = typing.get_type_hints(member)
        operations[name] = Operation(
            name=name,
            parameters=parameters,
            hints={p.name: hints.get(p.name) for p in parameters},
            returns=hints.get("return"),
        )
    return operations


OPERATIONS: dict[str, Operation] = _operations()


def route_path(name: str) -> str:
    """The route an operation is served at."""
    if name not in OPERATIONS:
        raise KeyError(name)
    return f"{ROUTE_PREFIX}/{name}"


def decode_args(operation: Operation, body: dict) -> dict:
    """The keyword arguments a request body carries for ``operation``.

    Refuses a body naming an argument the operation does not take, or
    missing one it requires.
    """
    if not isinstance(body, dict):
        raise WireError(f"{operation.name}: the request body must be a JSON object.")
    known = {p.name for p in operation.parameters}
    unknown = sorted(set(body) - known)
    if unknown:
        raise WireError(
            f"{operation.name}: unknown argument(s) {', '.join(unknown)}."
        )
    missing = [name for name in operation.required if name not in body]
    if missing:
        raise WireError(
            f"{operation.name}: missing argument(s) {', '.join(missing)}."
        )
    for name, value in body.items():
        hint = operation.hints.get(name)
        if not _accepts(hint, value):
            raise WireError(
                f"{operation.name}: argument {name!r} must be {_describe(hint)}."
            )
    return dict(body)


def _accepts(hint, value) -> bool:
    """Whether a JSON value can stand for a parameter typed ``hint``.

    Plain types, unions of them, and the elements of a typed list or object
    are checked; anything richer is let through, so a hint the wire does not
    model never refuses a valid call.
    """
    if hint is None:
        return True
    origin = typing.get_origin(hint)
    if origin is types.UnionType or origin is typing.Union:
        return any(_accepts(arg, value) for arg in typing.get_args(hint))
    if hint is type(None):
        return value is None
    if hint is bool:
        return isinstance(value, bool)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if hint is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if hint is str or hint is Path:
        return isinstance(value, str)
    if hint is dict or origin is dict:
        if not isinstance(value, dict):
            return False
        args = typing.get_args(hint)
        if len(args) != 2:
            return True
        return all(
            _accepts(args[0], key) and _accepts(args[1], item) for key, item in value.items()
        )
    if hint in (list, tuple) or origin in (list, tuple):
        if not isinstance(value, list):
            return False
        args = typing.get_args(hint)
        if origin is not list or len(args) != 1:
            return True
        return all(_accepts(args[0], item) for item in value)
    return True


# How the elements of a typed container read in a refusal.
_PLURALS = {str: "strings", int: "integers", float: "numbers", bool: "booleans"}


def _of(hint) -> str:
    """`` of strings`` for a container typed over a plain type; empty otherwise."""
    args = typing.get_args(hint)
    element = args[-1] if args else None
    return f" of {_PLURALS[element]}" if element in _PLURALS else ""


def _describe(hint) -> str:
    origin = typing.get_origin(hint)
    if origin is types.UnionType or origin is typing.Union:
        return " or ".join(_describe(arg) for arg in typing.get_args(hint))
    if hint is type(None):
        return "null"
    if hint is str or hint is Path:
        return "a string"
    if hint is bool:
        return "true or false"
    if hint is int:
        return "an integer"
    if hint is float:
        return "a number"
    if hint is dict or origin is dict:
        return "an object" + _of(hint)
    if hint in (list, tuple) or origin in (list, tuple):
        return "a list" + (_of(hint) if origin is list else "")
    return getattr(hint, "__name__", str(hint))


def encode(value):
    """A JSON-ready form of a value the local service returned."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): encode(item) for key, item in value.items()}
    return value


def decode(value, hint):
    """The typed value behind an encoded one, rebuilt by the type ``hint``."""
    if hint is None or hint is type(None) or hint is typing.Any:
        return value
    origin = typing.get_origin(hint)
    if origin in (types.UnionType, typing.Union):
        members = typing.get_args(hint)
        if value is None and type(None) in members:
            return None
        concrete = [m for m in members if m is not type(None)]
        return decode(value, concrete[0]) if len(concrete) == 1 else value
    if hint is Path:
        return Path(value)
    if origin is tuple:
        return tuple(decode(item, h) for item, h in zip(value, typing.get_args(hint)))
    if origin is list:
        (item_hint,) = typing.get_args(hint) or (typing.Any,)
        return [decode(item, item_hint) for item in value]
    if typing.is_typeddict(hint):
        key_hints = typing.get_type_hints(hint)
        return {
            key: decode(item, key_hints[key]) if key in key_hints else item
            for key, item in value.items()
        }
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        field_hints = typing.get_type_hints(hint)
        return hint(**{
            f.name: decode(value[f.name], field_hints[f.name])
            for f in dataclasses.fields(hint)
            if f.name in value
        })
    return value
