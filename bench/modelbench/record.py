"""Run-record schema: the one record each bench run writes.

`REQUIRED_FIELDS` maps each field to its type, or to a nested mapping for a
nested object. `validate_record` returns error strings that start with the
dotted field path; `check_record` raises RecordError with them.

The specflo version lives in `versions.specflo`; with `settings` it makes up
what must be equal across the arms of one comparison. `metrics`,
`diagnostics` and `telemetry` are dicts whose inner shape their writers own.
`lifecycle` carries how the run was watched and ended: where it ran (a herdr
pane or headless), the end detail and the level limits it ran under.

`OPTIONAL_FIELDS` are checked only when present.
"""

from __future__ import annotations

from typing import Any

from modelbench.arms import HARNESSES, LEVELS

END_REASONS = ("complete", "escalated", "timeout", "stalled", "harness-exit")
PLACEMENTS = ("herdr", "headless")

_NUMBER = (int, float)

REQUIRED_FIELDS: dict[str, Any] = {
    "arm": {"entry": str, "harness": str},
    "level": str,
    "run_index": int,
    "versions": {
        "harness": str,
        "engine_build": str,
        "specflo": str,
        "entry": str,
        "gguf_path": str,
    },
    "settings": {
        "sampling": dict,
        "context_window": int,
        "autonomy": str,
        "pass_cap": int,
    },
    "started_at": str,
    "ended_at": str,
    "end_reason": str,
    "valid": bool,
    "score": _NUMBER,
    "metrics": dict,
    "diagnostics": dict,
    "telemetry": dict,
    "logs": dict,
    "lifecycle": {
        "placement": str,
        "end_detail": str,
        "wall_clock_cap": _NUMBER,
        "stall_limit": _NUMBER,
    },
}

# Fields that may be absent; when present they must have this type.
OPTIONAL_FIELDS: dict[str, Any] = {
    "lifecycle.pane_id": (str, type(None)),
}

# Fields whose value must come from a fixed set.
ALLOWED_VALUES: dict[str, tuple[str, ...]] = {
    "arm.harness": HARNESSES,
    "level": LEVELS,
    "end_reason": END_REASONS,
    "lifecycle.placement": PLACEMENTS,
}


class RecordError(ValueError):
    """A run record that fails the schema. `errors` lists each failure."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _type_name(expected: Any) -> str:
    if isinstance(expected, tuple):
        return " or ".join("None" if t is type(None) else t.__name__ for t in expected)
    return expected.__name__


def _type_ok(value: Any, expected: Any) -> bool:
    # bool is an int subclass; only a bool field may hold one.
    if isinstance(value, bool):
        return expected is bool
    return isinstance(value, expected)


def _walk(node: Any, fields: dict[str, Any], prefix: str, errors: list[str]) -> None:
    for name, expected in fields.items():
        path = f"{prefix}{name}"
        if name not in node:
            errors.append(f"{path}: missing")
            continue
        value = node[name]
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                errors.append(f"{path}: expected dict, got {type(value).__name__}")
            else:
                _walk(value, expected, path + ".", errors)
            continue
        if not _type_ok(value, expected):
            errors.append(
                f"{path}: expected {_type_name(expected)}, got {type(value).__name__}"
            )
        elif path in ALLOWED_VALUES and value not in ALLOWED_VALUES[path]:
            allowed = ", ".join(ALLOWED_VALUES[path])
            errors.append(f"{path}: unknown {value!r} (allowed: {allowed})")


def validate_record(rec: Any) -> list[str]:
    """Return the schema errors of a run record; an empty list means valid."""
    if not isinstance(rec, dict):
        return [f"record: expected dict, got {type(rec).__name__}"]
    errors: list[str] = []
    _walk(rec, REQUIRED_FIELDS, "", errors)
    for path, expected in OPTIONAL_FIELDS.items():
        *parents, leaf = path.split(".")
        node = rec
        for name in parents:
            node = node.get(name) if isinstance(node, dict) else None
        if isinstance(node, dict) and leaf in node and not _type_ok(node[leaf], expected):
            errors.append(f"{path}: expected {_type_name(expected)}, got {type(node[leaf]).__name__}")
    return errors


def check_record(rec: Any) -> None:
    """Raise RecordError if the run record fails the schema."""
    errors = validate_record(rec)
    if errors:
        raise RecordError(errors)
