"""Run-record schema: the one record each bench run writes.

`REQUIRED_FIELDS` maps each field to its type, or to a nested mapping for a
nested object. `validate_record` returns error strings that start with the
dotted field path; `check_record` raises RecordError with them.

The specflo version lives in `versions.specflo`; with `settings` it makes up
what must be equal across the arms of one comparison. `metrics`,
`diagnostics` and `telemetry` are dicts whose inner shape their writers own.
"""

from __future__ import annotations

from typing import Any

from modelbench.arms import HARNESSES, LEVELS

END_REASONS = ("complete", "escalated", "timeout", "stalled", "harness-exit")

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
}

# Fields whose value must come from a fixed set.
ALLOWED_VALUES: dict[str, tuple[str, ...]] = {
    "arm.harness": HARNESSES,
    "level": LEVELS,
    "end_reason": END_REASONS,
}


class RecordError(ValueError):
    """A run record that fails the schema. `errors` lists each failure."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _type_name(expected: Any) -> str:
    if isinstance(expected, tuple):
        return " or ".join(t.__name__ for t in expected)
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
    return errors


def check_record(rec: Any) -> None:
    """Raise RecordError if the run record fails the schema."""
    errors = validate_record(rec)
    if errors:
        raise RecordError(errors)
