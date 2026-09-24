"""The pool configuration file: provider accounts, the member roster and named pools.

An admin writes ``pool.yaml`` ahead of time and the roster in it is complete:
nothing that is not declared can be leased. An account is a name, a cap on how
many leases may run through it at once, and the name of the environment
variable that holds its one API key. A member is what runs a role: the harness
command that starts it, what backs it, its capability labels, how many leases
it serves at once, and its egress class. Its name is also the name its agent
runs under, so it is one the agent host takes as an agent's name.

A local member runs against llama-swap on this host, so it names one concrete
model ID from the rig's llama-swap configuration and its class is ``local``.
A hosted member runs over the network, so it names a declared account and its
class is ``no-train`` or ``open``.

A member is of one of two kinds. The pool starts a process for each lease on
a member of kind ``started``, which is what an entry that names no kind is. A
member of kind ``console`` is a slot for a developer's own running agent: the
pool never starts its process, so it has no command, and one agent serves
one lease at a time, so its capacity is 1. What backs it, its labels and its
class are declared as any member's are, since it is matched and counted as
one while an agent is attached to it (see ``console``).

A named pool binds one agent definition to declared members, with the most
leases it grants at once, a default and a maximum idle limit, and an optional
``preempt_after``. The definitions are the markdown files in ``definitions/``
beside the pool file. A consumer asks for a pool by name and nothing else, so
whether each member meets the definition is settled here: a member that lacks
a label the definition needs, or is more open than it accepts, is refused when
the file is checked and never while a request waits.

Loading is strict: a key this module does not know is refused rather than
ignored, and every refusal names the entry and the field at fault. Accounts
members and pools are lists of named entries, not mappings, because YAML drops a
repeated mapping key without a word and a repeated name must be refused.

The pool file, the definitions and the teams in ``teams/`` make up the pool
directory, and ``load_pool_config`` reads the whole of it as one configuration.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..errors import SpecfloError
from . import matrix
from .definitions import (
    EGRESS_CLASSES,
    NOT_UTF8,
    AgentDefinition,
    DefinitionError,
    load_definition,
    not_a_file,
)

if TYPE_CHECKING:
    from .teams import Team

# What backs a member: llama-swap on this host, or a provider over the network.
LOCAL = "local"
HOSTED = "hosted"
BACKINGS: tuple[str, ...] = (LOCAL, HOSTED)

# The kinds of member: one the pool starts a process for at each lease, and a
# console, a slot a developer attaches a running agent host to.
STARTED = "started"
CONSOLE = "console"
KINDS: tuple[str, ...] = (STARTED, CONSOLE)

# The egress classes a hosted member may declare: every class but "local".
HOSTED_CLASSES: tuple[str, ...] = tuple(c for c in EGRESS_CLASSES if c != LOCAL)

# The hosted class whose members run with provider routing flags that deny
# data collection and demand zero data retention.
NO_TRAIN = "no-train"

# The vendor prefixes of the OpenRouter model IDs a no-train member may not
# name. pi sends the routing flags only from its OpenAI-completions client, and
# it reaches these vendors' models through another client, which sends none:
# the member would run with the flags in its file and in no request. The whole
# vendor is refused, not the model IDs pi lists today, so a model pi adds later
# is refused until it is known to send the flags.
NO_ROUTING_PREFIXES: tuple[str, ...] = ("anthropic/",)

# pi's name for the provider a hosted member runs through; a model may be
# written with it in front, "openrouter/vendor/model".
_PROVIDER_PREFIX = "openrouter/"

# Every key the file may carry, in the order they are written. An account has
# no field for a provider management key: such a key can mint spending keys,
# which is more authority than the pool needs.
SECTIONS: tuple[str, ...] = (
    "llama_swap", "models_file", "accounts", "members", "pools",
)
ACCOUNT_FIELDS: tuple[str, ...] = ("name", "cap", "key_env")
MEMBER_FIELDS: tuple[str, ...] = (
    "name", "kind", "command", "backing", "model", "account", "labels", "capacity", "egress",
)
# A pool has no field for a priority: any waiting request may take an idle
# lease from a pool that declares preempt_after, and none from one that does not.
POOL_FIELDS: tuple[str, ...] = (
    "name", "definition", "members", "size", "idle_default", "idle_max", "preempt_after",
)

# The pool file's name in a pool directory.
POOL_FILE = "pool.yaml"

# The directory beside the pool file that holds the agent definitions, one
# markdown file each; a pool binds a definition by the file's stem.
DEFINITIONS_DIR = "definitions"

# A limit is written as a whole number and a unit, "10m" or "4h". A bare
# number is refused because nothing says whether it counts seconds or minutes.
_DURATION = re.compile(r"\A([1-9][0-9]*)([smh])\Z")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

# The llama-swap ways of naming a model that the pool does not use. A profile
# is one server-wide rewrite table, and a selector picks the real model per
# request, after the pool has checked which models fit together.
_NOT_A_MODEL: tuple[str, ...] = ("profile", "selector")

# What the agent host takes as an agent's name. A member's agent runs under
# the member's name, so a member named otherwise never starts, and no status
# can be read for the lease written for it. A console's name is held to the
# same rule: members and agents share their names (see ``console``). The rule
# is the agent package's own, repeated here because this module imports
# nothing of that package.
AGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# The entry an error names when the fault is not in one account or member.
FILE = "file"

NOT_A_POOL_FILE = "not a regular file; put the pool's YAML file there."


class ConfigError(SpecfloError):
    """A pool configuration that cannot be used, with the entry and the field at fault."""

    def __init__(self, path: Path, entry: str, field: str, problem: str) -> None:
        super().__init__(f"{path}: {entry}: {field}: {problem}")
        self.path = path
        self.entry = entry
        self.field = field


@dataclass(frozen=True)
class Account:
    """One provider account. ``key_env`` names the variable, never the key."""

    name: str
    cap: int
    key_env: str


@dataclass(frozen=True)
class Member:
    """One roster entry. A local member has a ``model``, a hosted one an
    ``account``. A console has no ``command``: the pool never starts it."""

    name: str
    command: str
    backing: str
    labels: tuple[str, ...]
    capacity: int
    egress: str
    model: str | None = None
    account: str | None = None
    kind: str = STARTED
    # The ID the operator's models file holds a local member's model under,
    # which may be one of its llama-swap aliases; ``model`` is its llama-swap
    # ID. None where nothing was checked against that file.
    pi_model: str | None = None


@dataclass(frozen=True)
class Pool:
    """One named pool. ``size`` is the most leases it grants at once; the
    limits are in seconds, and ``preempt_after`` is None for a pool whose
    leases are never preempted."""

    name: str
    definition: str
    members: tuple[str, ...]
    size: int
    idle_default: int
    idle_max: int
    preempt_after: int | None = None


@dataclass(frozen=True)
class PoolConfig:
    """What ``pool.yaml`` declares, with the definitions beside it. ``swap`` is
    the llama-swap configuration at ``llama_swap``; both are None when the
    file names none. ``teams`` is filled only when the whole pool directory is
    loaded."""

    path: Path
    accounts: tuple[Account, ...] = ()
    members: tuple[Member, ...] = ()
    pools: tuple[Pool, ...] = ()
    definitions: tuple[AgentDefinition, ...] = ()
    llama_swap: Path | None = None
    swap: matrix.SwapConfig | None = None
    models_file: Path | None = None
    teams: tuple[Team, ...] = ()


def load_pool_config(directory: Path | str) -> tuple[PoolConfig, list[ConfigError]]:
    """The whole pool *directory* as one configuration, with every fault found
    in it: the pool file, the definitions and the teams.

    A team is checked against the pools that stand. One that names a pool
    refused for its own fault is not reported again, but it cannot stand
    without that pool.
    """
    # The teams module builds on this one, so it is imported only here.
    from . import teams

    directory = Path(directory)
    config, errors, pool_names = _check_pool_file(directory / POOL_FILE)
    if any(e.entry == FILE and e.field == "file" for e in errors):
        # Without the pool file no pool is declared, and every role of every
        # team would be reported for it.
        return config, errors
    standing = {p.name: p for p in config.pools}
    found, faults = teams.check_teams(
        directory / teams.TEAMS_DIR, standing, pool_names - set(standing)
    )
    return replace(config, teams=found), errors + faults


def load_pool_file(path: Path | str) -> PoolConfig:
    """The configuration in the file at *path*.

    Raises ``ConfigError`` for the first fault; ``check_pool_file`` gives all
    of them.
    """
    config, errors = check_pool_file(path)
    if errors:
        raise errors[0]
    return config


def check_pool_file(path: Path | str) -> tuple[PoolConfig, list[ConfigError]]:
    """The configuration in the file at *path*, with every fault found in it.

    The definitions are read from ``definitions/`` beside the file. An entry
    with a fault is left out of the configuration, so what is returned holds
    only entries that stand as written.
    """
    config, errors, _ = _check_pool_file(Path(path))
    return config, errors


def _check_pool_file(path: Path) -> tuple[PoolConfig, list[ConfigError], set[str]]:
    """As ``check_pool_file``, and with every name a pool entry gives, whether
    or not the pool stands.

    A pool file that is not a regular file is refused before it is opened: a
    named pipe would hold the reader until a writer comes.
    """
    if not_a_file(path):
        return PoolConfig(path), [ConfigError(path, FILE, "file", NOT_A_POOL_FILE)], set()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        problem = f"cannot be read ({exc.strerror})."
        return PoolConfig(path), [ConfigError(path, FILE, "file", problem)], set()
    except UnicodeDecodeError:
        return PoolConfig(path), [ConfigError(path, FILE, "file", NOT_UTF8)], set()
    except yaml.YAMLError:
        return PoolConfig(path), [ConfigError(path, FILE, "file", "not valid YAML.")], set()
    if data is None:
        data = {}
    if not isinstance(data, dict):
        problem = "not a mapping of " + ", ".join(SECTIONS) + "."
        return PoolConfig(path), [ConfigError(path, FILE, "file", problem)], set()

    errors: list[ConfigError] = []
    for key in data:
        if key not in SECTIONS:
            errors.append(ConfigError(
                path, FILE, str(key), "unknown key; expected one of " + ", ".join(SECTIONS) + "."
            ))

    account_entries = _entries(path, data, "accounts", errors)
    member_entries = _entries(path, data, "members", errors)
    pool_entries = _entries(path, data, "pools", errors)

    accounts = []
    for position, fields in account_entries:
        found: list[ConfigError] = []
        account = _account(_Entry(path, "account", "accounts", position, fields, found))
        errors.extend(found)
        if not found:
            accounts.append(account)
    # A member is checked against every name an account entry gives, so an
    # account refused for another fault is not also reported as undeclared.
    declared = set(_names(account_entries))

    needs_swap = any(isinstance(f, dict) and f.get("backing") == LOCAL for _, f in member_entries)
    llama_swap, swap = _llama_swap(path, data.get("llama_swap"), needs_swap, errors)
    faults = len(errors)
    models_file = _models_file(path, data.get("models_file"), needs_swap, errors)
    # A file already reported for its path is not opened, and a member is
    # checked against the operator's models only when there are some to
    # check it against.
    operator = None if len(errors) > faults else _operator_models(models_file)

    members = []
    for position, fields in member_entries:
        found = []
        member = _member(
            _Entry(path, "member", "members", position, fields, found), declared, swap, operator
        )
        errors.extend(found)
        if not found:
            members.append(member)

    definitions, definition_names = _definitions(path.parent / DEFINITIONS_DIR, errors)

    # A pool is checked against the members that stand alone under their name.
    # One it names that was refused for its own fault is not reported again,
    # but the pool cannot stand without it.
    member_names = _names(member_entries)
    standing = {m.name: m for m in members if member_names.count(m.name) == 1}
    pools = []
    for position, fields in pool_entries:
        found = []
        pool = _pool(
            _Entry(path, "pool", "pools", position, fields, found),
            definitions, definition_names, standing, set(member_names),
        )
        errors.extend(found)
        if not found and pool.definition in definitions and set(pool.members) <= set(standing):
            pools.append(pool)

    for kind, entries in (
        ("account", account_entries), ("member", member_entries), ("pool", pool_entries),
    ):
        errors.extend(_repeats(path, kind, entries))
    repeated = {e.entry for e in errors if e.field == "name"}
    return PoolConfig(
        path,
        tuple(a for a in accounts if f"account '{a.name}'" not in repeated),
        tuple(m for m in members if f"member '{m.name}'" not in repeated),
        tuple(p for p in pools if f"pool '{p.name}'" not in repeated),
        tuple(definitions.values()),
        llama_swap,
        swap,
        models_file,
    ), errors, set(_names(pool_entries))


def _entries(
    path: Path, data: dict, section: str, errors: list[ConfigError]
) -> list[tuple[int, object]]:
    """The entries under *section* with their positions, counted from 1."""
    value = data.get(section)
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(ConfigError(
            path, FILE, section, "must be a list of entries, each with a name."
        ))
        return []
    return list(enumerate(value, start=1))


def _names(entries: list[tuple[int, object]]) -> list[str]:
    """Every name the entries give, whether or not the entry stands."""
    names = [f.get("name") for _, f in entries if isinstance(f, dict)]
    return [n for n in names if isinstance(n, str)]


def _repeats(path: Path, kind: str, entries: list[tuple[int, object]]) -> list[ConfigError]:
    """One error for each name that more than one entry of *kind* carries."""
    names = _names(entries)
    problem = f"declared more than once; each {kind} needs its own name."
    return [
        ConfigError(path, f"{kind} '{name}'", "name", problem)
        for name in dict.fromkeys(names)
        if names.count(name) > 1
    ]


def _llama_swap(
    path: Path, value: object, needed: bool, errors: list[ConfigError]
) -> tuple[Path | None, matrix.SwapConfig | None]:
    """The llama-swap configuration the file names; a relative path is taken
    from the file's own directory."""
    if value is None:
        if needed:
            errors.append(ConfigError(
                path, FILE, "llama_swap",
                "required; a local member needs the path of the rig's llama-swap configuration.",
            ))
        return None, None
    if not isinstance(value, str) or not value.strip():
        errors.append(ConfigError(path, FILE, "llama_swap", "must be a path."))
        return None, None
    location = path.parent / Path(value).expanduser()
    try:
        return location, matrix.read(location)
    except SpecfloError as exc:
        errors.append(ConfigError(path, FILE, "llama_swap", f"{exc}."))
        return location, None


def _models_file(
    path: Path, value: object, needed: bool, errors: list[ConfigError]
) -> Path | None:
    """The operator's models file the pool file names; None when it names none.

    A local member's generated models file is a copy of this one, filtered to
    the model the member declares, so the pool has to be told where it is,
    and a file declaring a local member and no models file is refused: every
    lease of that member would fail at its start. A relative path is taken
    from the pool file's own directory.

    What is checked is the path and not the content: whether it is there, and
    whether it is a regular file this user can read. A named pipe passes every
    other check and then holds whoever opens it until a writer comes, and the
    daemon would wait with it, so a path that is not a regular file is refused
    before anything opens it.
    """
    if value is None:
        if needed:
            errors.append(ConfigError(
                path, FILE, "models_file",
                "required; a local member's models file is a copy of the operator's, "
                "and this is where that is.",
            ))
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(ConfigError(path, FILE, "models_file", "must be a path."))
        return None
    location = path.parent / Path(value).expanduser()
    problem = _unreadable(location)
    if problem is not None:
        errors.append(ConfigError(path, FILE, "models_file", f"{location}: {problem}."))
    return location


def _operator_models(location: Path | None) -> tuple[Path, frozenset[str]] | None:
    """The operator's models file and every model ID it holds, or None when
    there is nothing to check a member against.

    None for a pool file that names no such file, and for one whose content
    is not a provider map: neither leaves a set of models a member could be
    held to, and the copy made at the start of a lease is what refuses those.
    """
    if location is None:
        return None
    try:
        data = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return None
    held = set()
    for provider in providers.values():
        models = provider.get("models") if isinstance(provider, dict) else None
        for entry in models if isinstance(models, list) else ():
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                held.add(entry["id"])
    return (location, frozenset(held))


def _unreadable(location: Path) -> str | None:
    """Why the operator's models file cannot be read, or None when it can.

    ``is_file`` follows a link and says of what it leads to, so a link to a
    regular file passes and a link to a pipe does not.
    """
    if not location.exists():
        return "there is no file there"
    if not location.is_file():
        return "is not a regular file"
    if not os.access(location, os.R_OK):
        return "cannot be read"
    return None


def _definitions(
    folder: Path, errors: list[ConfigError]
) -> tuple[dict[str, AgentDefinition], set[str]]:
    """The definitions in *folder* by name, and every name a file there gives.

    A file that cannot be loaded is reported under its own path and name, so a
    pool that binds it is not also told the definition is undeclared.

    Each path a definition lists is checked against what the sandbox hides,
    as this process's environment names it and with the daemon root the one
    that holds the pool directory; a definition with a path at fault does not
    stand.
    """
    # The launch module builds on this one, so it is imported only here.
    from .launch import listed_path_fault

    loaded: dict[str, AgentDefinition] = {}
    names: set[str] = set()
    daemon_root = folder.parent.parent
    for file in sorted(folder.glob("*.md")):
        names.add(file.stem)
        label = f"definition '{file.stem}'"
        try:
            definition = load_definition(file)
        except DefinitionError as exc:
            problem = str(exc).removeprefix(f"{exc.path}: {exc.field}: ")
            errors.append(ConfigError(file, label, exc.field, problem))
            continue
        found = [
            fault for listed in definition.paths
            if (fault := listed_path_fault(listed, os.environ, daemon_root)) is not None
        ]
        errors.extend(ConfigError(file, label, "paths", fault) for fault in found)
        if not found:
            loaded[file.stem] = definition
    return loaded, names


class _Entry:
    """One account, member, pool or team role entry being checked: reads its fields
    and notes each fault against the entry's name, or its position when it has none."""

    def __init__(
        self, path: Path, kind: str, section: str, position: int, fields: object,
        errors: list[ConfigError],
    ) -> None:
        self.path = path
        self.fields = fields if isinstance(fields, dict) else {}
        self.errors = errors
        name = self.fields.get("name")
        named = isinstance(name, str) and name.strip()
        self.label = f"{kind} '{name}'" if named else f"{section}[{position}]"
        if not isinstance(fields, dict):
            self.fault("entry", "not a mapping of keys to values.")
        elif not named:
            self.fault("name", "required; a name other entries can refer to.")

    def fault(self, field: str, problem: str) -> None:
        self.errors.append(ConfigError(self.path, self.label, field, problem))

    def known(self, allowed: tuple[str, ...]) -> None:
        for key in self.fields:
            if key in _NOT_A_MODEL and "model" in allowed:
                self.fault(
                    key, "not used by the pool; a local member names one concrete "
                    "llama-swap model ID under 'model'."
                )
            elif key not in allowed:
                self.fault(str(key), "unknown key; expected one of " + ", ".join(allowed) + ".")

    def text(self, field: str, why: str) -> str:
        value = self.fields.get(field)
        if not isinstance(value, str) or not value.strip():
            self.fault(field, f"required; {why}.")
            return ""
        return value.strip()

    def at_least_one(self, field: str, why: str) -> int:
        value = self.fields.get(field)
        # A bool is an int to Python, and "cap: true" is not a count.
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            self.fault(field, f"must be a whole number of at least 1; {why}.")
            return 0
        return value

    def one_of(self, field: str, allowed: tuple[str, ...]) -> str:
        value = self.fields.get(field)
        if value not in allowed:
            expected = ", ".join(allowed)
            self.fault(field, f"unknown value {value!r}; expected one of {expected}.")
            return ""
        return value

    def strings(self, field: str) -> tuple[str, ...]:
        value = self.fields.get(field)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(i, str) and i for i in value):
            self.fault(field, "must be a list of strings.")
            return ()
        return tuple(value)

    def seconds(self, field: str, why: str) -> int:
        value = self.fields.get(field)
        match = _DURATION.match(value) if isinstance(value, str) else None
        if match is None:
            self.fault(
                field, "must be a whole number of at least 1 and a unit, s, m or h, "
                f"such as '10m'; {why}."
            )
            return 0
        return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def _account(entry: _Entry) -> Account:
    entry.known(ACCOUNT_FIELDS)
    return Account(
        name=str(entry.fields.get("name")),
        cap=entry.at_least_one("cap", "how many leases may run through the account at once"),
        key_env=entry.text(
            "key_env", "the name of the environment variable that holds the API key"
        ),
    )


def _member(
    entry: _Entry,
    accounts: set[str],
    swap: matrix.SwapConfig | None,
    operator: tuple[Path, frozenset[str]] | None = None,
) -> Member:
    entry.known(MEMBER_FIELDS)
    name = entry.fields.get("name")
    if isinstance(name, str) and name.strip() and not AGENT_NAME.match(name):
        entry.fault(
            "name", f"'{name}' cannot name an agent, and a member's agent runs under the "
            "member's name; it starts with a letter or a digit and holds only letters, "
            "digits, '.', '_' and '-'."
        )
    kind = entry.one_of("kind", KINDS) if "kind" in entry.fields else STARTED
    if kind == CONSOLE:
        command = ""
        if "command" in entry.fields:
            entry.fault(
                "command", "not for a console; the pool never starts a console's process, "
                "a developer attaches a running agent host to the slot."
            )
    else:
        command = entry.text("command", "the harness command that starts the member")
    backing = entry.one_of("backing", BACKINGS)
    labels = entry.strings("labels")
    capacity = entry.at_least_one("capacity", "how many leases the member serves at once")
    if kind == CONSOLE and capacity > 1:
        entry.fault(
            "capacity", f"{capacity} is more than a console serves; it is one running "
            "agent, so its capacity is 1."
        )
    egress = entry.one_of("egress", EGRESS_CLASSES)

    model = entry.fields.get("model")
    selected = model
    pi_model = None
    account = entry.fields.get("account")
    if backing == LOCAL:
        if not any(key in entry.fields for key in _NOT_A_MODEL):
            model = entry.text("model", "one concrete llama-swap model ID or alias")
            names: tuple[str, ...] = (model,) if model else ()
            if model and swap is not None:
                served = swap.config_id(model)
                if served is None:
                    entry.fault("model", _not_a_model_id(model, swap))
                    names = ()
                else:
                    names = swap.names(served)
            if names and operator is not None:
                pi_model = _held_as(entry, model, names, operator)
                selected = pi_model or model
            if names:
                model = names[0]
        if account is not None:
            entry.fault(
                "account", "not for a local member; only a hosted member runs through an account."
            )
        if egress and egress != LOCAL:
            entry.fault(
                "egress", f"a local member's data stays on this host, so its class is '{LOCAL}'."
            )
    elif backing == HOSTED:
        account = entry.text("account", "the declared account the member runs through")
        if account and account not in accounts:
            entry.fault("account", f"'{account}' is not a declared account.")
        if egress and egress not in HOSTED_CLASSES:
            entry.fault(
                "egress", "a hosted member sends data off this host, so its class is one of "
                + ", ".join(HOSTED_CLASSES) + "."
            )
        if egress == NO_TRAIN:
            if isinstance(model, str):
                _routed(entry, "model", model)
            for named in _command_models(command):
                _routed(entry, "command", named)
    if kind != CONSOLE:
        _one_model(entry, command, selected, hosted=backing == HOSTED)

    return Member(
        name=str(entry.fields.get("name")),
        command=command,
        backing=backing,
        labels=labels,
        capacity=capacity,
        egress=egress,
        model=model if isinstance(model, str) else None,
        account=account if isinstance(account, str) else None,
        kind=kind or STARTED,
        pi_model=pi_model,
    )


def _pool(
    entry: _Entry,
    definitions: dict[str, AgentDefinition],
    definition_names: set[str],
    members: dict[str, Member],
    member_names: set[str],
) -> Pool:
    """The pool in *entry*. *members* holds the members that stand; a name in
    *member_names* or *definition_names* alone has its fault reported already."""
    entry.known(POOL_FIELDS)
    bound = entry.text("definition", "the agent definition every member of the pool runs")
    if bound and bound not in definition_names:
        entry.fault(
            "definition", f"'{bound}' is not a declared definition; there is no "
            f"{DEFINITIONS_DIR}/{bound}.md beside the pool file."
        )
    definition = definitions.get(bound)

    listed = entry.strings("members")
    if not listed and not any(e.field == "members" for e in entry.errors):
        entry.fault("members", "required; a list of at least one declared member.")
    for name in dict.fromkeys(listed):
        if listed.count(name) > 1:
            entry.fault(
                "members", f"'{name}' is listed more than once; its capacity counts once."
            )
        if name not in member_names:
            entry.fault("members", f"'{name}' is not a declared member.")
        elif name in members and definition is not None:
            _fit(entry, members[name], definition)

    size = entry.at_least_one("size", "the most leases the pool grants at once")
    if size and listed and all(name in members for name in listed):
        capacity = sum(members[name].capacity for name in dict.fromkeys(listed))
        if size > capacity:
            entry.fault(
                "size", f"{size} is more than its members can serve at once; their "
                f"capacities add up to {capacity}."
            )

    idle_default = entry.seconds(
        "idle_default", "how long a lease may go without activity when the request does not say"
    )
    idle_max = entry.seconds("idle_max", "the longest idle limit a request may ask for")
    if idle_default and idle_max and idle_default > idle_max:
        entry.fault(
            "idle_default", f"{entry.fields['idle_default']} is above the maximum, "
            f"idle_max {entry.fields['idle_max']}."
        )
    preempt_after = None
    if entry.fields.get("preempt_after") is not None:
        preempt_after = entry.seconds(
            "preempt_after", "how long a lease must be idle before a waiting request may take it"
        )
        if preempt_after and idle_default and preempt_after >= idle_default:
            entry.fault(
                "preempt_after", f"{entry.fields['preempt_after']} is not shorter than the "
                f"default idle limit, idle_default {entry.fields['idle_default']}; the lease "
                "would expire before it could be taken."
            )

    return Pool(
        name=str(entry.fields.get("name")),
        definition=bound,
        members=listed,
        size=size,
        idle_default=idle_default,
        idle_max=idle_max,
        preempt_after=preempt_after or None,
    )


def _fit(entry: _Entry, member: Member, definition: AgentDefinition) -> None:
    """Note each way *member* falls short of what *definition* asks of a member."""
    for label in definition.needs:
        if label not in member.labels:
            entry.fault(
                "members", f"member '{member.name}' lacks the label '{label}' that "
                f"definition '{definition.name}' needs."
            )
    # The classes run strictest first, so a later one is more open.
    if EGRESS_CLASSES.index(member.egress) > EGRESS_CLASSES.index(definition.egress):
        entry.fault(
            "members", f"member '{member.name}' is class '{member.egress}', more open than "
            f"definition '{definition.name}' accepts, '{definition.egress}'."
        )


def _command_models(command: str) -> list[str]:
    """Every model *command* selects, as ``--model <value>`` or ``--model=<value>``.

    The command is split the way the launch splits it. One that cannot be
    split selects nothing here; it cannot start a member either.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return []
    models = [after for arg, after in zip(argv, argv[1:]) if arg == "--model"]
    return models + [arg.removeprefix("--model=") for arg in argv if arg.startswith("--model=")]


def _one_model(entry: _Entry, command: str, model: object, *, hosted: bool = False) -> None:
    """Note a fault unless the member runs the one model it names.

    A local member's generated models file holds the model it declares and
    no other, and the co-residency ledger accounts for that one, so a command
    selecting another model would run the member on a model neither knows
    about. A local member's command that selects none is no fault: the
    generated file leaves one model to resolve to. A *hosted* member's
    generated file pins no model, so a hosted member that declares a model
    must select it in its command; one that selects none would run the
    provider's default. A member that names none anywhere is refused,
    because then nothing says which model that is.

    A declaration already at fault is left alone, and so is a command the
    shell rules cannot split; one fault is enough, and an unsplittable
    command selects nothing this could read.
    """
    if any(error.field == "model" for error in entry.errors):
        return
    try:
        shlex.split(command)
    except ValueError:
        # A command the shell rules cannot split says nothing about which
        # model it selects; it is the launch that refuses it.
        return
    declared = model.strip() if isinstance(model, str) and model.strip() else None
    selected = _command_models(command)
    if declared is None:
        if not selected:
            entry.fault(
                "model", "required; neither the declaration nor the command names a "
                "model, and a member runs the one model it names."
            )
        return
    if hosted and not selected:
        entry.fault(
            "command", f"selects no model, and the member declares '{declared}'; a hosted "
            "member's generated models file pins none, so its command must select "
            f"'{declared}' with --model."
        )
        return
    for named in selected:
        if _same_model(named, declared):
            continue
        entry.fault(
            "command", f"selects '{named}', and the member declares '{declared}'; a member "
            "runs the one model it names, and its generated models file holds that one alone."
        )
        return


def _same_model(named: str, declared: str) -> bool:
    """Whether two ways of writing a model name the same model. pi matches a
    model ID without regard to case, and names a provider's model with the
    provider in front of it."""
    def bare(value: str) -> str:
        return value.strip().lower().removeprefix(_PROVIDER_PREFIX)

    return bare(named) == bare(declared)


def _routed(entry: _Entry, field: str, model: str) -> None:
    """Note a fault on *field* unless the no-train flags reach the provider
    when a member runs *model*. pi matches a model ID without regard to case."""
    full = model.strip().lower().removeprefix(_PROVIDER_PREFIX)
    if "/" not in full:
        # pi takes a name with no vendor as a pattern and picks the model
        # itself, so nothing here can tell which client the member would use.
        entry.fault(
            field, f"'{model}' is a partial model name, which can resolve to a model whose "
            f"client does not send the {NO_TRAIN} flags; the full 'vendor/model' ID is required."
        )
    elif full.startswith(NO_ROUTING_PREFIXES):
        entry.fault(
            field, f"'{model}' is a model pi reaches through a client that sends no provider "
            f"routing, so the {NO_TRAIN} flags would not reach the provider. Name another "
            "vendor's model, or declare the member 'open'."
        )


def _held_as(
    entry: _Entry, declared: str, names: tuple[str, ...], operator: tuple[Path, frozenset[str]]
) -> str | None:
    """The ID the operator's models file holds the member's model under, or
    None with a fault noted.

    *names* are every name llama-swap answers the model under, and the file
    may hold it under any one of them. The name the member declares wins when
    the file holds it. Otherwise the file must hold exactly one of the names:
    two entries for one model may differ in every setting, and nothing says
    which the member runs with.
    """
    location, held = operator
    if declared in held:
        return declared
    found = [name for name in names if name in held]
    if len(found) == 1:
        return found[0]
    if not found:
        entry.fault("model", _not_in_operator_models(declared, location))
    else:
        entry.fault(
            "model", f"'{declared}' is held in the operator's models file at {location} under "
            + " and ".join(f"'{name}'" for name in found)
            + "; declare the one the member runs."
        )
    return None


def _not_in_operator_models(model: str, location: Path) -> str:
    """Why a local member cannot run the model it declares.

    The message names that model alone. What else the operator holds is not
    the member's business, and a roster is read by more people than the
    operator.
    """
    return (
        f"'{model}' is not in the operator's models file at {location}, and a local "
        "member's models file is a copy of that one, filtered to this model."
    )


def _not_a_model_id(model: str, swap: matrix.SwapConfig) -> str:
    if model in swap.vars:
        return (
            f"'{model}' is a matrix var, not a model ID; name the model it stands for, "
            f"'{swap.vars[model]}'."
        )
    return (
        f"'{model}' is not a model ID in the llama-swap configuration; a profile or a "
        "selector is not one either. Expected one of " + ", ".join(swap.model_ids) + "."
    )
