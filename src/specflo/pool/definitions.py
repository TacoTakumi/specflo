"""Agent definitions: a role as a harness-neutral markdown file.

A definition says what a role is, not what runs it. The YAML front matter
names the role, the tools and skills it may use, the commands it must not
run, the environment variables and credentials it may be given, the
capability labels it needs from a member, the most open egress class it
accepts, and whether it wants the project's context files. The body is the
system prompt.

Loading is strict: a definition is the only limit on what a member is handed,
so a key this module does not know is refused rather than ignored, and every
refusal names the file and the field at fault.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..errors import SpecfloError

# Egress classes, strictest first: data stays on this host; a hosted provider
# that neither keeps nor trains on prompts; a hosted provider that may.
EGRESS_CLASSES: tuple[str, ...] = ("local", "no-train", "open")

# What a definition accepts when it does not say: sending private code to a
# provider that trains on it cannot be undone, so "open" is always an opt-in.
DEFAULT_EGRESS = "no-train"

# The front-matter keys that hold a list of strings.
LIST_FIELDS: tuple[str, ...] = ("tools", "skills", "deny", "env", "credentials", "needs")

# Every front-matter key a definition may carry, in the order they are written.
FIELDS: tuple[str, ...] = ("role", *LIST_FIELDS, "egress", "project_context")

# The pseudo-fields an error names when the fault is not in one key.
FRONT_MATTER = "front matter"
BODY = "body"

# The problem of a file whose bytes are not UTF-8. Every file of the pool
# directory is read as UTF-8, whatever the locale of the daemon says.
NOT_UTF8 = "not UTF-8 text; save the file as UTF-8."

# The problem of an entry under the name of a file that is a folder, a named
# pipe, a socket or a device. Such an entry is never opened: a named pipe that
# nothing writes to holds its reader for ever.
NOT_A_FILE = "not a regular file; put a markdown file there, or take the entry away."

# Front matter opens on the first line and closes on the next line that is
# only "---"; a later "---" rule belongs to the prompt.
_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*$\n?(.*)\Z", re.DOTALL | re.MULTILINE)


class DefinitionError(SpecfloError):
    """A definition file that cannot be used, with the file and the field at fault."""

    def __init__(self, path: Path, field: str, problem: str) -> None:
        super().__init__(f"{path}: {field}: {problem}")
        self.path = path
        self.field = field


@dataclass(frozen=True)
class AgentDefinition:
    """One role. ``name`` is the file's stem, the name pools bind it by."""

    name: str
    role: str
    prompt: str
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    credentials: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()
    egress: str = DEFAULT_EGRESS
    project_context: bool = False


def not_a_file(path: Path) -> bool:
    """Whether what is at ``path`` is something other than a regular file. A
    symlink counts as what it leads to; a path with nothing at it is not one,
    and the read of it says that the file is missing."""
    return path.exists() and not path.is_file()


def load_definition(path: Path) -> AgentDefinition:
    """The definition in the markdown file at ``path``.

    Raises ``DefinitionError`` for an entry that is not a regular file, which
    is not opened, and for a file that cannot be read, has no valid front
    matter, carries an unknown key, a missing role, a wrongly typed value or
    an unknown egress class, or has an empty body.
    """
    if not_a_file(path):
        raise DefinitionError(path, "file", NOT_A_FILE)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DefinitionError(path, "file", f"cannot be read ({exc.strerror}).") from exc
    except UnicodeDecodeError as exc:
        raise DefinitionError(path, "file", NOT_UTF8) from exc
    return parse_definition(text, path)


def parse_definition(text: str, path: Path) -> AgentDefinition:
    """The definition in ``text``; ``path`` names it and is what an error reports."""
    match = _FRONT_MATTER.match(text)
    if match is None:
        raise DefinitionError(path, FRONT_MATTER, "missing; the file must open with a '---' block.")
    try:
        fields = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise DefinitionError(path, FRONT_MATTER, "not valid YAML.") from exc
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        raise DefinitionError(path, FRONT_MATTER, "not a mapping of keys to values.")

    for key in fields:
        if key not in FIELDS:
            raise DefinitionError(
                path, str(key), "unknown key; expected one of " + ", ".join(FIELDS) + "."
            )

    role = fields.get("role")
    if not isinstance(role, str) or not role.strip():
        raise DefinitionError(path, "role", "required; say in one line what the role does.")

    lists = {key: _string_list(path, key, fields.get(key)) for key in LIST_FIELDS}

    egress = fields.get("egress", DEFAULT_EGRESS)
    if egress not in EGRESS_CLASSES:
        raise DefinitionError(
            path, "egress", f"unknown class {egress!r}; expected one of "
            + ", ".join(EGRESS_CLASSES) + "."
        )

    project_context = fields.get("project_context", False)
    if not isinstance(project_context, bool):
        raise DefinitionError(path, "project_context", "must be true or false.")

    prompt = match.group(2).strip()
    if not prompt:
        raise DefinitionError(path, BODY, "empty; the body is the role's system prompt.")

    return AgentDefinition(
        name=path.stem,
        role=role.strip(),
        prompt=prompt,
        egress=egress,
        project_context=project_context,
        **lists,
    )


def _string_list(path: Path, key: str, value: object) -> tuple[str, ...]:
    """``value`` as a tuple of strings; an absent key is the empty tuple."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise DefinitionError(path, key, "must be a list of strings.")
    return tuple(value)


def serialise_definition(definition: AgentDefinition) -> str:
    """The markdown file of *definition*, as an admin writes one by hand.

    The keys stand in the order of ``FIELDS``, a list on one line in brackets.
    A list with nothing in it is left out, and so is ``project_context`` unless
    it is true; the egress class is always written, since it is what a reader
    looks for first. What comes back from ``parse_definition`` is *definition*.
    """
    values = {"role": definition.role}
    values.update({key: list(getattr(definition, key)) for key in LIST_FIELDS})
    values["egress"] = definition.egress
    values["project_context"] = definition.project_context
    lines = []
    for key in FIELDS:
        value = values[key]
        if value == [] or value is False:
            continue
        # One key to a call, so that every key is one line of a block mapping:
        # a list goes in brackets, and a value YAML would read as something
        # else, or as a comment, is quoted.
        lines.append(yaml.safe_dump(
            {key: value}, default_flow_style=None if isinstance(value, list) else False,
            width=float("inf"), allow_unicode=True,
        ))
    return "---\n" + "".join(lines) + "---\n\n" + definition.prompt.strip() + "\n"
