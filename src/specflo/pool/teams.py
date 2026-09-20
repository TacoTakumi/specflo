"""Team files: a named set of roles, each a pool and a count.

A team is a markdown file in ``teams/`` beside the pool file, written like an
agent definition by the admin or through the dashboard. The file's stem is the
team's name. The YAML front matter lists the roles; each role names a declared
pool and how many of that pool's leases the role takes. The body is free notes
for whoever leads the team, and may be empty.

A team is leased all or nothing, so a role that can never be granted would
leave its request waiting for ever. Such a role is refused here, when the
configuration is checked: one that names an undeclared pool, a count below 1,
or a count above the most leases its pool grants at once.

Loading is strict, as it is for the pool file: a key this module does not know
is refused rather than ignored, and every refusal names the team and the role
at fault. Roles are a list of named entries, not a mapping, because YAML drops
a repeated mapping key without a word and a repeated name must be refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import ConfigError, Pool, _Entry, _names
from .definitions import _FRONT_MATTER, NOT_UTF8

# The directory beside the pool file that holds the teams, one markdown file
# each; a request names a team by the file's stem.
TEAMS_DIR = "teams"

# Every key a team file and a role may carry, in the order they are written.
# A team has no field for a lead: the orchestrator that holds the lease leads.
FIELDS: tuple[str, ...] = ("roles",)
ROLE_FIELDS: tuple[str, ...] = ("name", "pool", "count")

# The pseudo-field an error names when the fault is not in one key.
FRONT_MATTER = "front matter"


@dataclass(frozen=True)
class Role:
    """One role of a team: ``count`` leases from the pool named ``pool``."""

    name: str
    pool: str
    count: int


@dataclass(frozen=True)
class Team:
    """One team. ``name`` is the file's stem, the name a request asks for."""

    name: str
    roles: tuple[Role, ...]
    notes: str = ""


def check_teams(
    folder: Path, pools: dict[str, Pool], refused: set[str]
) -> tuple[tuple[Team, ...], list[ConfigError]]:
    """The teams in *folder* in name order, with every fault found in them.

    *pools* holds the pools that stand; a name in *refused* is a pool whose
    own fault is reported already. A team with a fault is left out, and so is
    one that cannot stand without a refused pool.
    """
    found: list[Team] = []
    errors: list[ConfigError] = []
    for file in sorted(folder.glob("*.md")):
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as exc:
            problem = f"cannot be read ({exc.strerror})."
            errors.append(ConfigError(file, f"team '{file.stem}'", "file", problem))
            continue
        except UnicodeDecodeError:
            errors.append(ConfigError(file, f"team '{file.stem}'", "file", NOT_UTF8))
            continue
        team, faults = check_team(text, file, pools, refused)
        errors.extend(faults)
        if not faults and all(role.pool in pools for role in team.roles):
            found.append(team)
    return tuple(found), errors


def check_team(
    text: str, path: Path, pools: dict[str, Pool], refused: set[str] = frozenset()
) -> tuple[Team, list[ConfigError]]:
    """The team in *text*, with every fault found in it; *path* names it and
    is what an error reports."""
    label = f"team '{path.stem}'"
    errors: list[ConfigError] = []
    team = Team(name=path.stem, roles=())

    match = _FRONT_MATTER.match(text)
    if match is None:
        problem = "missing; the file must open with a '---' block."
        return team, [ConfigError(path, label, FRONT_MATTER, problem)]
    try:
        fields = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return team, [ConfigError(path, label, FRONT_MATTER, "not valid YAML.")]
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        problem = "not a mapping of keys to values."
        return team, [ConfigError(path, label, FRONT_MATTER, problem)]

    for key in fields:
        if key not in FIELDS:
            errors.append(ConfigError(
                path, label, str(key), "unknown key; expected one of " + ", ".join(FIELDS) + "."
            ))

    listed = fields.get("roles")
    if not isinstance(listed, list) or not listed:
        errors.append(ConfigError(
            path, label, "roles",
            "required; a list of at least one role, each with a name, a pool and a count.",
        ))
        return team, errors

    entries = list(enumerate(listed, start=1))
    roles = [
        _role(_Entry(path, f"{label} role", f"{label} roles", position, entry, errors),
              pools, refused)
        for position, entry in entries
    ]
    names = _names(entries)
    for name in dict.fromkeys(names):
        if names.count(name) > 1:
            errors.append(ConfigError(
                path, f"{label} role '{name}'", "name",
                "declared more than once; each role needs its own name.",
            ))
    return Team(name=path.stem, roles=tuple(roles), notes=match.group(2).strip()), errors


def _role(entry: _Entry, pools: dict[str, Pool], refused: set[str]) -> Role:
    entry.known(ROLE_FIELDS)
    pool = entry.text("pool", "the declared pool the role's leases come from")
    if pool and pool not in pools and pool not in refused:
        entry.fault("pool", f"'{pool}' is not a declared pool.")
    count = entry.at_least_one("count", "how many of the pool's leases the role takes")
    if count and pool in pools and count > pools[pool].size:
        entry.fault(
            "count", f"{count} is more than pool '{pool}' grants at once, {pools[pool].size}."
        )
    return Role(name=str(entry.fields.get("name")), pool=pool, count=count)


def serialise_team(team: Team) -> str:
    """The markdown file of *team*, as an admin writes one by hand.

    A role is one line of the ``roles`` list, its keys in the order of
    ``ROLE_FIELDS``, and a value YAML would read as something else, or as a
    comment, is quoted. The notes are the body, and a team with none has no
    body. What comes back from ``check_team`` is *team*.
    """
    lines = ["roles:\n" if team.roles else "roles: []\n"]
    for role in team.roles:
        fields = {key: getattr(role, key) for key in ROLE_FIELDS}
        lines.append("  - " + yaml.safe_dump(
            fields, default_flow_style=True, sort_keys=False, width=float("inf"),
            allow_unicode=True,
        ))
    notes = team.notes.strip()
    return "---\n" + "".join(lines) + "---\n" + (f"\n{notes}\n" if notes else "")
