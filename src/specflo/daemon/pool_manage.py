"""The management pages of the pool directory: the developer's browser edits what an admin edits.

An agent definition is one markdown file in the pool directory, and the files
stay the source of truth. The pages list them, and create, edit and delete
one. Every page is built from the files as they are at that request, never
from the configuration the running pool holds, so a hand edit shows on the
next read. A save writes the text a hand would write, through the serialiser
that is the inverse of the definition parser.

A file changes in one way only, in ``put_file``. The whole pool directory is
checked first with the candidate in place, by the check ``serve pool
validate`` runs, on a copy that stands beside the directory so that a relative
path in the pool file reads the same. With any fault, the candidate's own or
another's, nothing changes and the faults are what the page shows beside the
form, with what was posted kept: a directory with a fault could not be put in
force, so a save into one is refused unless it is the save that mends it.
With none, the file is replaced in one step or removed, the change is audited
with the acting identity, and the running pool reads its directory again.

A definition that a pool binds is not deleted, and the refusal names the pool.

A team is one markdown file in the teams folder, and its pages are the
siblings of the definitions' pages: the same list, form, save and delete, on a
router of their own. The form holds the roles one to a line, a name, a pool
and a count. A role on a pool that is not declared, or with a count above what
its pool grants at once, is the validator's to refuse, and the refusal names
the team and the role. A team is deleted with a lease of it out: the members
are those of its pools, which stand, and the lease ends as it would have.

The pages and the posts are the developer's alone. Every post runs the
session-secret guard first, as every mutating route of the web UI does, then
refuses any other identity. Accounts, members and pools have no page here, no
page runs an agent, and nothing a member wrote is shown: a prompt on these
pages is the text of the definition file.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from ..pool.cli_admin import pool_dir
from ..pool.config import DEFINITIONS_DIR, ConfigError, Pool, load_pool_config
from ..pool.definitions import (
    BODY, DEFAULT_EGRESS, EGRESS_CLASSES, LIST_FIELDS, AgentDefinition, DefinitionError,
    load_definition, serialise_definition,
)
from ..pool.teams import ROLE_FIELDS, TEAMS_DIR, Role, Team, check_team, serialise_team
from .pool_routes import NO_POOL, reload_pool
from .routes import audit
from .web import (
    DEFINITIONS_PATH, DELETE_DEFINITION_PATH, DELETE_TEAM_PATH, EDIT_DEFINITION_PATH,
    EDIT_TEAM_PATH, NEW_DEFINITION_PATH, NEW_TEAM_PATH, TEAMS_PATH, current_session,
    form_fields, render, session_refused, session_secret,
)

# The one identity that is shown these pages and may post to them.
MANAGER = "developer"

# What a save and a delete are audited as.
SAVED = "definition_save"
DELETED = "definition_delete"
TEAM_SAVED = "team_save"
TEAM_DELETED = "team_delete"

# A name becomes a file name, so it is letters, digits, "-" and "_", and
# opens with a letter or a digit: no separator, no leading dot, no "..".
_PLAIN_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]*\Z")
NOT_PLAIN = (
    "required; letters, digits, '-' and '_', with a letter or a digit first. "
    "It is the name of the file, and the name a pool binds the definition by."
)

TEAM_NOT_PLAIN = (
    "required; letters, digits, '-' and '_', with a letter or a digit first. "
    "It is the name of the file, and the name a request asks for the team by."
)
# What the form says of a line of its roles that is not one.
NOT_A_ROLE = (
    "line {number}, '{line}': a role is a name, a pool and a count with spaces between, "
    "such as 'critic critics 1'."
)

# The copy of the pool directory a candidate is checked in, beside the directory.
CANDIDATE_PREFIX = ".pool-candidate-"

# The fields of the definition form that a fault is shown beside; a fault of
# the file in anything else stands above the form with the other files' faults.
FORM_FIELDS: tuple[str, ...] = ("name", "role", *LIST_FIELDS, "egress", "project_context", BODY)

# The fields of a team file whose fault is shown beside the roles of the team
# form; a fault of the file in anything else stands above the form.
TEAM_FORM_FIELDS: tuple[str, ...] = ("roles", "entry", *ROLE_FIELDS)

# One change to the pool directory at a time.
_changing = threading.Lock()


# --- the one way a file of the pool directory changes ---------------------------


def _at_home(fault: ConfigError, copy: Path, directory: Path) -> ConfigError:
    """*fault*, found in the *copy*, as it reads for the pool *directory* itself."""
    problem = str(fault).removeprefix(f"{fault.path}: {fault.entry}: {fault.field}: ")
    path = Path(str(fault.path).replace(str(copy), str(directory), 1))
    return ConfigError(
        path, fault.entry, fault.field, problem.replace(str(copy), str(directory))
    )


def _real_in(copy: Path, relative: Path) -> Path:
    """The path of *relative* in the *copy*, with no symlink left on it, so
    that what is written or removed there is in the copy and nowhere else.

    The copy keeps the symlinks of the directory as symlinks, and a symlink to
    a place outside still leads there. Each folder on the way that is one
    becomes a real folder holding a copy of what the symlink showed, empty for
    a symlink that shows no folder; a symlink in it with a relative path is
    made to lead where it led from the folder that was shown. A symlink where
    the file is to be is removed: the symlink, never what it leads to."""
    at = copy
    for part in relative.parts[:-1]:
        at = at / part
        if not at.is_symlink():
            continue
        try:
            shown = at.resolve(strict=True)
        except (OSError, RuntimeError):
            shown = None
        at.unlink()
        if shown is None or not shown.is_dir():
            at.mkdir()
            continue
        shutil.copytree(shown, at, symlinks=True)
        for folder, folders, files in os.walk(at):
            for entry in (Path(folder, name) for name in (*folders, *files)):
                if entry.is_symlink() and not os.path.isabs(leads := os.readlink(entry)):
                    entry.unlink()
                    entry.symlink_to(shown / Path(folder).relative_to(at) / leads)
    target = copy / relative
    if target.is_symlink():
        target.unlink()
    return target


def check_candidate(directory: Path, relative: Path, text: str | None) -> list[ConfigError]:
    """Every fault of the pool *directory* as it would be with the file at
    *relative* holding *text*, or gone for a *text* of None. Nothing in the
    directory changes: the check runs on a copy beside it, and is the one
    ``serve pool validate`` runs.

    The check writes, removes and makes nothing outside the copy. That holds
    where the folder of the file, or the file, is a symlink to a place outside
    the directory: the real file there is read and is never touched."""
    copy = Path(tempfile.mkdtemp(prefix=CANDIDATE_PREFIX, dir=directory.parent))
    try:
        shutil.copytree(directory, copy, symlinks=True, dirs_exist_ok=True)
        target = _real_in(copy, relative)
        if text is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        _, faults = load_pool_config(copy)
        return [_at_home(fault, copy, directory) for fault in faults]
    finally:
        shutil.rmtree(copy, ignore_errors=True)


def _replace(target: Path, text: str) -> None:
    """Put *text* at *target* in one step: a reader sees the old file or the new one."""
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
        os.chmod(name, mode)
        os.replace(name, target)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def put_file(
    app, identity: str, relative: Path, text: str | None, operation: str, named: str
) -> list[ConfigError]:
    """Write *text* to the file at *relative* in the pool directory of the
    daemon application *app*, or remove the file for a *text* of None; the
    faults that refused it, none for a change that was made.

    With a fault nothing changes. A change that was made is audited under
    *identity* as *operation* on *named*, and then the running pool reads the
    directory again; what that reload said is kept on the application, as
    after any reload.
    """
    directory = pool_dir(app.state.root)
    with _changing:
        faults = check_candidate(directory, relative, text)
        if faults:
            return faults
        if text is None:
            (directory / relative).unlink()
        else:
            _replace(directory / relative, text)
        audit(app.state.root, identity, operation, None, named)
        reload_pool(app, identity)
    return []


# --- what the pages show ----------------------------------------------------------


@dataclass(frozen=True)
class DefinitionRow:
    """One definition file: what it holds, or the fault that keeps it from
    loading, and the pools of the pool file that bind it."""

    name: str
    definition: AgentDefinition | None
    fault: str | None
    pools: tuple[str, ...]


def _files(directory: Path) -> dict[str, Path]:
    """The definition files of the pool *directory* by name, in name order."""
    return {file.stem: file for file in sorted((directory / DEFINITIONS_DIR).glob("*.md"))}


def binding(directory: Path, name: str) -> tuple[str, ...]:
    """The pools that bind the definition *name*, in the files as they are now."""
    config, _ = load_pool_config(directory)
    return tuple(pool.name for pool in config.pools if pool.definition == name)


def definition_rows(directory: Path) -> tuple[list[DefinitionRow], list[ConfigError]]:
    """The definition files of the pool *directory* as they are now, and every
    fault of the directory."""
    config, faults = load_pool_config(directory)
    rows = []
    for name, file in _files(directory).items():
        definition = fault = None
        try:
            definition = load_definition(file)
        except DefinitionError as exc:
            fault = f"{exc.field}: " + str(exc).removeprefix(f"{exc.path}: {exc.field}: ")
        bound = tuple(pool.name for pool in config.pools if pool.definition == name)
        rows.append(DefinitionRow(name=name, definition=definition, fault=fault, pools=bound))
    return rows, faults


def form_values(definition: AgentDefinition) -> dict[str, str]:
    """*definition* as the form holds it: a list is one item to a line."""
    values = {key: "\n".join(getattr(definition, key)) for key in LIST_FIELDS}
    return {
        **values, "name": definition.name, "role": definition.role,
        "egress": definition.egress, "prompt": definition.prompt,
        "project_context": "on" if definition.project_context else "",
    }


def posted_values(fields: dict[str, str], name: str) -> dict[str, str]:
    """What a form posted for the definition *name*, with a browser's line ends made plain."""
    keys = ("role", *LIST_FIELDS, "egress", "project_context", "prompt")
    values = {key: fields.get(key, "").replace("\r\n", "\n").replace("\r", "\n") for key in keys}
    return {**values, "name": name}


def posted_definition(values: dict[str, str]) -> AgentDefinition:
    """The definition the posted *values* describe. Whether it is a valid one
    is the validator's matter: an empty role or body is written as it is."""
    lists = {
        key: tuple(line.strip() for line in values[key].splitlines() if line.strip())
        for key in LIST_FIELDS
    }
    return AgentDefinition(
        name=values["name"], role=" ".join(values["role"].split()),
        prompt=values["prompt"].strip(), egress=values["egress"],
        project_context=bool(values["project_context"]), **lists,
    )


# --- the pages ------------------------------------------------------------------

pages = APIRouter()


def _not_manager(identity: str) -> Response | None:
    """The 403 page for any identity but the manager's, or None for the manager."""
    if identity == MANAGER:
        return None
    return render(
        "error.html", status_code=403, identity=identity,
        message=f"The pool directory is managed from these pages by the {MANAGER} identity "
        f"only, and this session is the {identity} identity's.",
    )


def _no_directory(request: Request, identity: str) -> Response | None:
    """The 409 page of a daemon root with no pool directory, or None with one."""
    if pool_dir(request.app.state.root).is_dir():
        return None
    return render("error.html", status_code=409, identity=identity, message=NO_POOL)


def _missing(identity: str, name: str, kind: str = "definition") -> Response:
    return render(
        "missing.html", status_code=404, identity=identity, message=f"No {kind} {name!r}."
    )


def _list(request: Request, identity: str, status_code: int = 200, **refusal) -> Response:
    """The list of the definition files; with a *refusal*, what was not done and why."""
    directory = pool_dir(request.app.state.root)
    if not directory.is_dir():
        return render(
            "pool_definitions.html", status_code=status_code, identity=identity, rows=None,
            no_pool=NO_POOL,
        )
    rows, faults = definition_rows(directory)
    return render(
        "pool_definitions.html", status_code=status_code, identity=identity, rows=rows,
        faults=[str(fault) for fault in faults],
        reload_faults=[str(fault) for fault in getattr(request.app.state, "pool_errors", ())],
        session=session_secret(request), **refusal,
    )


def _form(
    request: Request, identity: str, name: str | None, values: dict[str, str],
    faults: list[ConfigError] = (), name_fault: str | None = None,
) -> Response:
    """The form of the definition *name*, or of a new one for None, holding
    *values*. With *faults* or a *name_fault* it is a save that was refused:
    a fault of this file in a field of the form stands beside the field, and
    every other fault above the form."""
    target = pool_dir(request.app.state.root) / DEFINITIONS_DIR / f"{values['name']}.md"
    beside: dict[str, list[str]] = {"name": [name_fault]} if name_fault else {}
    above = []
    for fault in faults:
        if fault.path == target and fault.field in FORM_FIELDS:
            problem = str(fault).removeprefix(f"{fault.path}: {fault.entry}: {fault.field}: ")
            beside.setdefault(fault.field, []).append(problem)
        else:
            above.append(str(fault))
    return render(
        "pool_definition_form.html", status_code=400 if faults or name_fault else 200,
        identity=identity, name=name, values=values, field_errors=beside, errors=above,
        egress_classes=EGRESS_CLASSES, list_fields=LIST_FIELDS, body_field=BODY,
        session=session_secret(request),
    )


@pages.get(DEFINITIONS_PATH, include_in_schema=False)
def definitions_page(request: Request, identity: str = Depends(current_session)) -> Response:
    """The definition files as they are now, each with the pools that bind it."""
    return _not_manager(identity) or _list(request, identity)


@pages.get(NEW_DEFINITION_PATH, include_in_schema=False)
def new_definition_page(request: Request, identity: str = Depends(current_session)) -> Response:
    """The empty form of a new definition."""
    refused = _not_manager(identity) or _no_directory(request, identity)
    if refused is not None:
        return refused
    blank = AgentDefinition(name="", role="", prompt="", egress=DEFAULT_EGRESS)
    return _form(request, identity, None, form_values(blank))


@pages.get(EDIT_DEFINITION_PATH, include_in_schema=False)
def edit_definition_page(
    request: Request, name: str, identity: str = Depends(current_session)
) -> Response:
    """The form of the definition *name*, holding what its file says now."""
    refused = _not_manager(identity) or _no_directory(request, identity)
    if refused is not None:
        return refused
    file = _files(pool_dir(request.app.state.root)).get(name)
    if file is None:
        return _missing(identity, name)
    try:
        definition = load_definition(file)
    except DefinitionError as exc:
        return render(
            "error.html", status_code=409, identity=identity,
            message=f"{exc} The form holds only what a definition file can say, so "
            "correct the file by hand, or save the definition anew over it.",
        )
    return _form(request, identity, name, form_values(definition))


async def _guarded(request: Request, identity: str) -> tuple[dict[str, str], Response | None]:
    """The fields a form posted, and the refusal of a post that may not change
    the pool directory: the session-secret guard first, then the identity, then
    a daemon root with no pool directory."""
    fields = await form_fields(request)
    refused = (
        session_refused(request, fields, identity)
        or _not_manager(identity)
        or _no_directory(request, identity)
    )
    return fields, refused


@pages.post(DEFINITIONS_PATH, include_in_schema=False)
async def create_definition(
    request: Request, identity: str = Depends(current_session)
) -> Response:
    """Write a new definition file under the name the form gives, and come
    back to the list. A name that is not a plain one, or that a file has, is
    refused before anything is checked."""
    fields, refused = await _guarded(request, identity)
    if refused is not None:
        return refused
    name = fields.get("name", "").strip()
    values = posted_values(fields, name)
    if _PLAIN_NAME.match(name) is None:
        return _form(request, identity, None, values, name_fault=NOT_PLAIN)
    if name in _files(pool_dir(request.app.state.root)):
        return _form(
            request, identity, None, values,
            name_fault=f"there is a definition '{name}' already; edit that one, or take "
            "another name.",
        )
    return await _save(request, identity, None, values)


@pages.post(EDIT_DEFINITION_PATH, include_in_schema=False)
async def save_definition(
    request: Request, name: str, identity: str = Depends(current_session)
) -> Response:
    """Write what the form holds over the file of the definition *name*, and
    come back to the list. The name is the path's: a save renames nothing."""
    fields, refused = await _guarded(request, identity)
    if refused is not None:
        return refused
    if name not in _files(pool_dir(request.app.state.root)):
        return _missing(identity, name)
    return await _save(request, identity, name, posted_values(fields, name))


async def _save(
    request: Request, identity: str, name: str | None, values: dict[str, str]
) -> Response:
    """Save the definition *values* describe; *name* is None for a new one."""
    relative = Path(DEFINITIONS_DIR) / f"{values['name']}.md"
    text = serialise_definition(posted_definition(values))
    faults = await run_in_threadpool(
        put_file, request.app, identity, relative, text, SAVED, values["name"]
    )
    if faults:
        return _form(request, identity, name, values, faults)
    return RedirectResponse(DEFINITIONS_PATH, status_code=303)


@pages.post(DELETE_DEFINITION_PATH, include_in_schema=False)
async def delete_definition(
    request: Request, name: str, identity: str = Depends(current_session)
) -> Response:
    """Remove the file of the definition *name*, and come back to the list. A
    definition that a pool binds stays, and the refusal names the pool."""
    _, refused = await _guarded(request, identity)
    if refused is not None:
        return refused
    directory = pool_dir(request.app.state.root)
    if name not in _files(directory):
        return _missing(identity, name)
    pools = await run_in_threadpool(binding, directory, name)
    if pools:
        bound = ", ".join(f"pool '{pool}'" for pool in pools)
        return _list(
            request, identity, status_code=409,
            refused=f"The definition '{name}' was not deleted: {bound} "
            f"{'binds' if len(pools) == 1 else 'bind'} the definition '{name}'. Bind "
            "another definition in the pool file first.",
        )
    relative = Path(DEFINITIONS_DIR) / f"{name}.md"
    faults = await run_in_threadpool(put_file, request.app, identity, relative, None, DELETED, name)
    if faults:
        return _list(
            request, identity, status_code=409,
            refused=f"The definition '{name}' was not deleted: the pool directory would "
            "not stand without it.",
            refused_faults=[str(fault) for fault in faults],
        )
    return RedirectResponse(DEFINITIONS_PATH, status_code=303)


# --- the teams: what the pages show ---------------------------------------------


@dataclass(frozen=True)
class TeamRow:
    """One team file: what it holds when the form can hold it, and its faults
    as the check of the pool directory reports them."""

    name: str
    team: Team | None
    faults: tuple[str, ...]


def _team_files(directory: Path) -> dict[str, Path]:
    """The team files of the pool *directory* by name, in name order."""
    return {file.stem: file for file in sorted((directory / TEAMS_DIR).glob("*.md"))}


def role_lines(team: Team) -> str:
    """The roles of *team* as the form holds them: one role to a line."""
    return "\n".join(f"{role.name} {role.pool} {role.count}" for role in team.roles)


def posted_roles(text: str) -> tuple[tuple[Role, ...], list[str]]:
    """The roles the lines of *text* say, and what is wrong with each line that
    is not a role. A name may have spaces in it; a pool and a count have none.
    Whether a role can stand is the validator's matter."""
    roles, problems = [], []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.rsplit(None, 2)
        if len(parts) == 3 and re.fullmatch(r"-?[0-9]+", parts[2]):
            roles.append(Role(name=" ".join(parts[0].split()), pool=parts[1], count=int(parts[2])))
        else:
            problems.append(NOT_A_ROLE.format(number=number, line=line.strip()))
    return tuple(roles), problems


def read_team(file: Path, pools: dict[str, Pool]) -> Team | None:
    """The team in *file* when the form can hold it, else None. A role on a
    pool that is not among *pools*, or with a count its pool does not grant,
    is one the form holds, since a save there mends it; a file that says what
    the form has no field for is corrected by hand."""
    try:
        team, faults = check_team(file.read_text(encoding="utf-8"), file, pools)
    except (OSError, UnicodeDecodeError):
        return None
    mended_by_hand = [fault for fault in faults if fault.field not in ("pool", "count")]
    if mended_by_hand or posted_roles(role_lines(team)) != (team.roles, []):
        return None
    return team


def team_rows(directory: Path) -> tuple[list[TeamRow], list[ConfigError]]:
    """The team files of the pool *directory* as they are now, and every fault
    of the directory."""
    config, faults = load_pool_config(directory)
    pools = {pool.name: pool for pool in config.pools}
    rows = []
    for name, file in _team_files(directory).items():
        own = tuple(
            str(fault).removeprefix(f"{fault.path}: ") for fault in faults if fault.path == file
        )
        rows.append(TeamRow(name=name, team=read_team(file, pools), faults=own))
    return rows, faults


def posted_team(fields: dict[str, str], name: str) -> dict[str, str]:
    """What a form posted for the team *name*, with a browser's line ends made plain."""
    keys = ("roles", "notes")
    values = {key: fields.get(key, "").replace("\r\n", "\n").replace("\r", "\n") for key in keys}
    return {**values, "name": name}


# --- the teams: the pages -------------------------------------------------------

team_pages = APIRouter()


def _team_list(request: Request, identity: str, status_code: int = 200, **refusal) -> Response:
    """The list of the team files; with a *refusal*, what was not done and why."""
    directory = pool_dir(request.app.state.root)
    if not directory.is_dir():
        return render(
            "pool_teams.html", status_code=status_code, identity=identity, rows=None,
            no_pool=NO_POOL,
        )
    rows, faults = team_rows(directory)
    return render(
        "pool_teams.html", status_code=status_code, identity=identity, rows=rows,
        faults=[str(fault) for fault in faults],
        reload_faults=[str(fault) for fault in getattr(request.app.state, "pool_errors", ())],
        session=session_secret(request), **refusal,
    )


def _team_form(
    request: Request, identity: str, name: str | None, values: dict[str, str],
    faults: list[ConfigError] = (), beside: dict[str, list[str]] | None = None,
) -> Response:
    """The form of the team *name*, or of a new one for None, holding *values*.
    With *faults*, or a fault *beside* a field, it is a save that was refused:
    a fault of this file in its roles stands beside them with the team and
    the role it names, and every other fault above the form."""
    target = pool_dir(request.app.state.root) / TEAMS_DIR / f"{values['name']}.md"
    beside = dict(beside or {})
    above = []
    for fault in faults:
        if fault.path == target and fault.field in TEAM_FORM_FIELDS:
            problem = str(fault).removeprefix(f"{fault.path}: ")
            beside.setdefault("roles", []).append(problem)
        else:
            above.append(str(fault))
    return render(
        "pool_team_form.html", status_code=400 if faults or beside else 200,
        identity=identity, name=name, values=values, field_errors=beside, errors=above,
        session=session_secret(request),
    )


@team_pages.get(TEAMS_PATH, include_in_schema=False)
def teams_page(request: Request, identity: str = Depends(current_session)) -> Response:
    """The team files as they are now, each with its roles."""
    return _not_manager(identity) or _team_list(request, identity)


@team_pages.get(NEW_TEAM_PATH, include_in_schema=False)
def new_team_page(request: Request, identity: str = Depends(current_session)) -> Response:
    """The empty form of a new team."""
    refused = _not_manager(identity) or _no_directory(request, identity)
    if refused is not None:
        return refused
    return _team_form(request, identity, None, {"name": "", "roles": "", "notes": ""})


@team_pages.get(EDIT_TEAM_PATH, include_in_schema=False)
def edit_team_page(
    request: Request, name: str, identity: str = Depends(current_session)
) -> Response:
    """The form of the team *name*, holding what its file says now."""
    refused = _not_manager(identity) or _no_directory(request, identity)
    if refused is not None:
        return refused
    directory = pool_dir(request.app.state.root)
    file = _team_files(directory).get(name)
    if file is None:
        return _missing(identity, name, "team")
    config, _ = load_pool_config(directory)
    team = read_team(file, {pool.name: pool for pool in config.pools})
    if team is None:
        return render(
            "error.html", status_code=409, identity=identity,
            message=f"{file}: the form holds only the roles of a team, each a name, a pool "
            "and a count, and its notes, and this file does not read as those. Correct "
            "the file by hand.",
        )
    values = {"name": name, "roles": role_lines(team), "notes": team.notes}
    return _team_form(request, identity, name, values)


@team_pages.post(TEAMS_PATH, include_in_schema=False)
async def create_team(request: Request, identity: str = Depends(current_session)) -> Response:
    """Write a new team file under the name the form gives, and come back to
    the list. A name that is not a plain one, or that a file has, is refused
    before anything is checked."""
    fields, refused = await _guarded(request, identity)
    if refused is not None:
        return refused
    name = fields.get("name", "").strip()
    values = posted_team(fields, name)
    if _PLAIN_NAME.match(name) is None:
        return _team_form(request, identity, None, values, beside={"name": [TEAM_NOT_PLAIN]})
    if name in _team_files(pool_dir(request.app.state.root)):
        taken = f"there is a team '{name}' already; edit that one, or take another name."
        return _team_form(request, identity, None, values, beside={"name": [taken]})
    return await _save_team(request, identity, None, values)


@team_pages.post(EDIT_TEAM_PATH, include_in_schema=False)
async def save_team(
    request: Request, name: str, identity: str = Depends(current_session)
) -> Response:
    """Write what the form holds over the file of the team *name*, and come
    back to the list. The name is the path's: a save renames nothing."""
    fields, refused = await _guarded(request, identity)
    if refused is not None:
        return refused
    if name not in _team_files(pool_dir(request.app.state.root)):
        return _missing(identity, name, "team")
    return await _save_team(request, identity, name, posted_team(fields, name))


async def _save_team(
    request: Request, identity: str, name: str | None, values: dict[str, str]
) -> Response:
    """Save the team *values* describe; *name* is None for a new one. A line
    that is not a role is refused before anything is checked."""
    roles, problems = posted_roles(values["roles"])
    if problems:
        return _team_form(request, identity, name, values, beside={"roles": problems})
    relative = Path(TEAMS_DIR) / f"{values['name']}.md"
    text = serialise_team(Team(name=values["name"], roles=roles, notes=values["notes"]))
    faults = await run_in_threadpool(
        put_file, request.app, identity, relative, text, TEAM_SAVED, values["name"]
    )
    if faults:
        return _team_form(request, identity, name, values, faults)
    return RedirectResponse(TEAMS_PATH, status_code=303)


@team_pages.post(DELETE_TEAM_PATH, include_in_schema=False)
async def delete_team(
    request: Request, name: str, identity: str = Depends(current_session)
) -> Response:
    """Remove the file of the team *name*, and come back to the list. A lease
    of the team that is out ends as it would have: its members are those of
    the pools, which stand."""
    _, refused = await _guarded(request, identity)
    if refused is not None:
        return refused
    if name not in _team_files(pool_dir(request.app.state.root)):
        return _missing(identity, name, "team")
    relative = Path(TEAMS_DIR) / f"{name}.md"
    faults = await run_in_threadpool(
        put_file, request.app, identity, relative, None, TEAM_DELETED, name
    )
    if faults:
        return _team_list(
            request, identity, status_code=409,
            refused=f"The team '{name}' was not deleted: the pool directory would not "
            "stand without it.",
            refused_faults=[str(fault) for fault in faults],
        )
    return RedirectResponse(TEAMS_PATH, status_code=303)
