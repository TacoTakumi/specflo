"""Read the rig's llama-swap configuration: model IDs, matrix vars, and which
models may be loaded together.

The pool only ever *reads* this file; llama-swap owns loading, eviction and
card assignment. The ``matrix`` block gives short names (``vars``) to model
IDs and lists ``sets``: expressions over ``&``, ``|``, ``()`` and ``+name``
(another set) whose expansions are the combinations llama-swap will keep
loaded side by side. Any subset of a combination is allowed, and a model in no
set only runs alone, so a group of models fits when one expansion of one set
holds all of them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..errors import SpecfloError

# A name is a var or a full model ID: anything but blanks and the operators.
_TOKEN = re.compile(r"\s*([&|()]|[^\s&|()]+)")

# One expansion of a set: the model IDs of one allowed combination.
Combination = frozenset[str]


@dataclass(frozen=True)
class SwapConfig:
    """What the pool needs from a llama-swap configuration."""

    model_ids: tuple[str, ...]
    vars: dict[str, str]
    combinations: tuple[Combination, ...]
    # Every alias a model is given, to the model ID it names. llama-swap
    # answers a model under its ID and under each of these.
    aliases: dict[str, str] = field(default_factory=dict)

    def config_id(self, name: str) -> str | None:
        """The model ID llama-swap serves *name* under: *name* itself when it
        is one, the model it is an alias of, or None."""
        if name in self.model_ids:
            return name
        return self.aliases.get(name)

    def names(self, model_id: str) -> tuple[str, ...]:
        """Every name llama-swap answers *model_id* under, the ID first."""
        return (model_id, *(alias for alias, named in self.aliases.items() if named == model_id))

    def model_id(self, name: str) -> str:
        """The model ID behind *name*, which is a matrix var or already a
        model ID."""
        if name in self.vars:
            return self.vars[name]
        if name in self.model_ids:
            return name
        raise SpecfloError(f"llama-swap configuration has no model or matrix var '{name}'")

    def fits(self, models: Iterable[str]) -> bool:
        """Whether *models* (model IDs or vars) may all be loaded together: one
        expansion of one set holds every one of them. A model repeated is one
        model, and a single model always fits."""
        wanted = {self.model_id(m) for m in models}
        if len(wanted) <= 1:
            return True
        return any(wanted <= combo for combo in self.combinations)


def read(path: Path | str) -> SwapConfig:
    """Load the llama-swap configuration at *path*.

    A path that is not a regular file is refused before it is opened: a named
    pipe would hold the reader until a writer comes.
    """
    path = Path(path)
    if path.exists() and not path.is_file():
        raise SpecfloError(f"llama-swap configuration {path} is not a regular file")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise SpecfloError(f"cannot read llama-swap configuration {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise SpecfloError(f"llama-swap configuration {path} is not UTF-8 text") from exc
    except yaml.YAMLError as exc:
        raise SpecfloError(f"llama-swap configuration {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecfloError(f"llama-swap configuration {path} is not a mapping")

    models = data.get("models") or {}
    model_ids = tuple(str(m) for m in models)
    aliases = {
        str(alias): str(model)
        for model, entry in models.items()
        if isinstance(entry, dict) and isinstance(entry.get("aliases"), list)
        for alias in entry["aliases"]
    }
    block = data.get("matrix") or {}
    names = {str(k): str(v) for k, v in (block.get("vars") or {}).items()}
    for var, model in names.items():
        if model not in model_ids:
            raise SpecfloError(
                f"llama-swap configuration {path}: matrix var '{var}' names "
                f"'{model}', which is not under models"
            )
    sets = {str(k): str(v) for k, v in (block.get("sets") or {}).items()}
    expander = _Expander(sets, names, set(model_ids), path)
    combos: list[Combination] = []
    for name in sets:
        combos.extend(expander.expand(name))
    return SwapConfig(model_ids, names, tuple(combos), aliases)


class _Expander:
    """Expands set expressions into their combinations of model IDs.

    ``&`` binds tighter than ``|``. ``a | b`` is either combination; ``x & y``
    joins each combination of ``x`` with each of ``y``.
    """

    def __init__(self, sets: dict[str, str], names: dict[str, str], models: set[str], path: Path):
        self._sets = sets
        self._names = names
        self._models = models
        self._path = path
        self._done: dict[str, list[Combination]] = {}
        self._open: list[str] = []  # sets being expanded, to catch a +name loop

    def expand(self, name: str) -> list[Combination]:
        if name in self._done:
            return self._done[name]
        if name in self._open:
            raise self._error(name, "refers back to itself")
        self._open.append(name)
        combos, rest = self._either(name, _TOKEN.findall(self._sets[name]))
        if rest:
            raise self._error(name, f"has an unexpected '{rest[0]}'")
        self._open.pop()
        self._done[name] = combos
        return combos

    def _error(self, name: str, what: str) -> SpecfloError:
        return SpecfloError(
            f"llama-swap configuration {self._path}: matrix set '{name}' "
            f"({self._sets[name]}) {what}"
        )

    def _either(self, name: str, tokens: list[str]) -> tuple[list[Combination], list[str]]:
        combos, tokens = self._both(name, tokens)
        while tokens and tokens[0] == "|":
            more, tokens = self._both(name, tokens[1:])
            combos = combos + more
        return combos, tokens

    def _both(self, name: str, tokens: list[str]) -> tuple[list[Combination], list[str]]:
        combos, tokens = self._atom(name, tokens)
        while tokens and tokens[0] == "&":
            more, tokens = self._atom(name, tokens[1:])
            combos = [left | right for left in combos for right in more]
        return combos, tokens

    def _atom(self, name: str, tokens: list[str]) -> tuple[list[Combination], list[str]]:
        if not tokens:
            raise self._error(name, "ends where a name is expected")
        head, rest = tokens[0], tokens[1:]
        if head == "(":
            combos, rest = self._either(name, rest)
            if not rest or rest[0] != ")":
                raise self._error(name, "has an unclosed '('")
            return combos, rest[1:]
        if head in "&|)":
            raise self._error(name, f"has an unexpected '{head}'")
        if head.startswith("+"):
            ref = head[1:]
            if ref not in self._sets:
                raise self._error(name, f"refers to an unknown set '{ref}'")
            return self.expand(ref), rest
        if head in self._names:
            return [frozenset({self._names[head]})], rest
        if head in self._models:
            return [frozenset({head})], rest
        raise self._error(name, f"names '{head}', which is neither a matrix var nor a model ID")
