"""The pool configuration file: provider accounts, the member roster and named pools.

An admin writes ``pool.yaml`` ahead of time and the roster in it is complete:
nothing that is not declared can be leased. An account is a name, a cap on how
many leases may run through it at once, and the name of the environment
variable that holds its one API key. A member is what runs a role: the harness
command that starts it, what backs it, its capability labels, how many leases
it serves at once, and its egress class.

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

import re
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..errors import SpecfloError
from . import matrix
from .definitions import EGRESS_CLASSES, AgentDefinition, DefinitionError, load_definition

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
SECTIONS: tuple[str, ...] = ("llama_swap", "accounts", "members", "pools")
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

# The entry an error names when the fault is not in one account or member.
FILE = "file"


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
    or not the pool stands."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        problem = f"cannot be read ({exc.strerror})."
        return PoolConfig(path), [ConfigError(path, FILE, "file", problem)], set()
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

    members = []
    for position, fields in member_entries:
        found = []
        member = _member(_Entry(path, "member", "members", position, fields, found), declared, swap)
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


def _definitions(
    folder: Path, errors: list[ConfigError]
) -> tuple[dict[str, AgentDefinition], set[str]]:
    """The definitions in *folder* by name, and every name a file there gives.

    A file that cannot be loaded is reported under its own path and name, so a
    pool that binds it is not also told the definition is undeclared.
    """
    loaded: dict[str, AgentDefinition] = {}
    names: set[str] = set()
    for file in sorted(folder.glob("*.md")):
        names.add(file.stem)
        try:
            loaded[file.stem] = load_definition(file)
        except DefinitionError as exc:
            problem = str(exc).removeprefix(f"{exc.path}: {exc.field}: ")
            errors.append(ConfigError(file, f"definition '{file.stem}'", exc.field, problem))
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


def _member(entry: _Entry, accounts: set[str], swap: matrix.SwapConfig | None) -> Member:
    entry.known(MEMBER_FIELDS)
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
    account = entry.fields.get("account")
    if backing == LOCAL:
        if not any(key in entry.fields for key in _NOT_A_MODEL):
            model = entry.text("model", "one concrete llama-swap model ID")
            if model and swap is not None and model not in swap.model_ids:
                entry.fault("model", _not_a_model_id(model, swap))
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
