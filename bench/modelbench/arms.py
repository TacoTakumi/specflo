"""Arm config: the llama-swap entries, harnesses and levels a bench run may use.

A run is one arm (an entry id and a harness), one level and one run index.
`validate_run` refuses a run with any of these missing or unknown, before
anything talks to llama-swap.

An optional `engines` mapping holds per-engine settings. `limit_factor` scales
a level's wall-clock cap and stall limit for runs on that engine's entries, so
a slower engine gets the same headroom as the one the limits were set on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HARNESSES = ("pi", "claude-code")
LEVELS = ("quick", "fast", "full")
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "arms.yaml"


class ArmError(ValueError):
    """A bad arm, run or config field. `field` names it; the message starts with it."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(f"{field}: {message}")


@dataclass(frozen=True)
class Config:
    entries: dict[str, dict[str, Any]]
    harnesses: tuple[str, ...]
    levels: dict[str, dict[str, Any]]
    engines: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class Run:
    entry: str
    harness: str
    level: str
    run_index: int


def _check_names(field: str, names: Any, allowed: tuple[str, ...]) -> None:
    if not names:
        raise ArmError(field, "missing or empty")
    for name in names:
        if name not in allowed:
            raise ArmError(field, f"unknown {name!r} (allowed: {', '.join(allowed)})")


def load_config(path: Path | str = DEFAULT_CONFIG) -> Config:
    """Read and check an arm config file; raise ArmError naming the bad field."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        raise ArmError("config", "expected a mapping")
    entries = data.get("entries")
    if not isinstance(entries, dict) or not entries:
        raise ArmError("entries", "missing or empty mapping of llama-swap entry ids")
    for entry_id, spec in entries.items():
        if not isinstance(spec, dict) or not spec.get("engine"):
            raise ArmError(f"entries.{entry_id}.engine", "missing")
    harnesses = data.get("harnesses")
    if not isinstance(harnesses, list):
        raise ArmError("harnesses", "missing list")
    _check_names("harnesses", harnesses, HARNESSES)
    levels = data.get("levels")
    if not isinstance(levels, dict):
        raise ArmError("levels", "missing mapping")
    _check_names("levels", levels, LEVELS)
    engines = data.get("engines") or {}
    if not isinstance(engines, dict):
        raise ArmError("engines", "expected a mapping of engine names")
    for engine, spec in engines.items():
        if not isinstance(spec, dict):
            raise ArmError(f"engines.{engine}", "expected a mapping")
        factor = spec.get("limit_factor", 1)
        if isinstance(factor, bool) or not isinstance(factor, (int, float)) or factor <= 0:
            raise ArmError(f"engines.{engine}.limit_factor", f"expected a positive number, got {factor!r}")
    return Config(
        entries=entries,
        harnesses=tuple(harnesses),
        levels={name: spec or {} for name, spec in levels.items()},
        engines=engines,
    )


def limit_factor(config: Config, entry: str) -> float:
    """The factor that scales the level limits for runs on this entry's engine (1 when unset)."""
    engine = config.entries.get(entry, {}).get("engine")
    return float(config.engines.get(engine, {}).get("limit_factor", 1))


def _known(field: str, value: Any, known: Any) -> str:
    if value is None or value == "":
        raise ArmError(field, "missing")
    if value not in known:
        raise ArmError(field, f"unknown {value!r} (known: {', '.join(known)})")
    return value


def validate_run(
    config: Config, *, entry: Any, harness: Any, level: Any, run_index: Any
) -> Run:
    """Return the Run, or raise ArmError naming the first missing or unknown field."""
    entry = _known("entry", entry, config.entries)
    harness = _known("harness", harness, config.harnesses)
    level = _known("level", level, config.levels)
    if run_index is None:
        raise ArmError("run_index", "missing")
    if isinstance(run_index, bool) or not isinstance(run_index, int) or run_index < 0:
        raise ArmError("run_index", f"expected a non-negative int, got {run_index!r}")
    return Run(entry=entry, harness=harness, level=level, run_index=run_index)
