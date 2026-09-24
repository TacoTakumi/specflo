"""A member's pi configuration directory: what it loads, and where its class holds.

A hosted member's prompts leave this host, and its egress class says who may
see them. OpenRouter picks the provider that serves each request, and a request
can narrow that choice: ``data_collection: "deny"`` keeps it to providers that
collect no data, ``zdr: true`` to endpoints that retain none. OpenRouter joins
both with the account's own settings, so they can tighten the choice and never
loosen it. pi sends whatever ``compat.openRouterRouting`` holds in its
``models.json`` as that part of a request, unchanged.

One limit, seen in pi 0.85.1: only pi's OpenAI-completions client sends the
routing. pi reaches OpenRouter's Anthropic models through its Anthropic-messages
client instead, which drops it, so for those models the flags are in the file
and never in a request. The account's own privacy settings are what holds there.

pi reads ``models.json`` from its configuration directory, so each lease gets a
directory of its own, generated here, and the member's environment points pi at
it. A ``no-train`` member's file carries both flags; an ``open`` member's
carries neither.

A local member gets a directory too, and it carries no routing: nothing it
sends leaves this host, so there is no provider to ask anything of. What it
carries instead is the operator's own local provider, copied in. A local member
runs against the rig's llama-swap, and the provider that reaches it - its base
URL, its API flavour, its key and the compatibility fields that make pi and
that server agree - is declared in the operator's models file, which the
sandbox hides. So the file is read at lease start and one provider written out,
holding exactly the model the member declares with the fields the operator
curated for it: the context window that model really has, its maximum tokens,
its thinking levels. A member cannot then reach for a model it was not given,
and it cannot be given one the operator does not have.

The generated directory stands in for the user's ``~/.pi/agent`` whole, so none
of the user's settings, extensions, skills, stored credentials or sessions reach
the member. The one thing pi still needs from there is a key, and the file
names the variable that holds the key of the member's account; the key itself
is never written. pi keeps the lease's sessions under the directory, and they go
with it when the lease ends.

The directory also holds the member's git global configuration: the user.name
and user.email the daemon's git reports outside any repository, and nothing
else of the operator's, so a member can commit under the operator's name
without being handed a credential helper, an include or a signing key.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from copy import deepcopy
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from .config import HOSTED, Account, Member
from .launch import AGENT_DIR_ENV, GITCONFIG_FILE, LaunchError, member_account

# The file in a pi configuration directory that declares providers and models.
MODELS_FILE = "models.json"

# pi's name for the provider every hosted member runs through. It is built in
# to pi, so the file adds to its entry and declares no model of its own.
PROVIDER = "openrouter"

# What each hosted egress class asks of OpenRouter's provider routing. "open"
# asks for nothing, so its file carries no routing at all.
ROUTING: dict[str, dict[str, object]] = {
    "no-train": {"data_collection": "deny", "zdr": True},
    "open": {},
}

# The directory pi discovers a configuration directory's own skills in, one
# directory per skill, named by the skill. A member's copies go here.
SKILLS_DIR = "skills"

# Where pi looks first for the programs its tools run, under its
# configuration directory, and the programs it fetches from the internet
# when it finds them nowhere. The sandbox hides the operator's copies.
TOOLS_DIR = "bin"
TOOLS = ("fd", "rg")

# Where the operator's pi configuration directory is when the environment
# names none.
_DEFAULT_AGENT_DIR = Path(".pi") / "agent"

# The names a local member's provider may be reached at: the forwarder inside
# its sandbox answers on them.
_LOOPBACK = frozenset({"127.0.0.1", "localhost"})


def operator_skills(environ: Mapping[str, str]) -> Path:
    """The operator's own skills directory, which a member's copies come from.

    It is the ``skills`` of the operator's pi configuration directory, named
    by the environment or under the home where pi keeps it. The directory
    holds real skills and links into other checkouts alike; what is done
    about that is the copy's business, not this one's.
    """
    named = environ.get(AGENT_DIR_ENV)
    home = Path(environ.get("HOME") or Path.home())
    return (Path(named) if named else home / _DEFAULT_AGENT_DIR) / SKILLS_DIR


def operator_tools(environ: Mapping[str, str]) -> Path:
    """The ``bin`` of the operator's pi configuration directory, where pi keeps
    the programs it fetched for its tools."""
    return operator_skills(environ).parent / TOOLS_DIR


def models_config(member: Member, account: Account) -> dict[str, object]:
    """What ``models.json`` holds for the hosted *member* on *account*.

    The routing sits on the provider, where pi merges it into every model of
    that provider: the flags then hold for whichever model the member's command
    selects. It is repeated on the entry of the model the member names. The
    key is named as ``$VARIABLE``, which pi resolves from its environment.
    """
    provider: dict[str, object] = {"apiKey": f"${account.key_env}"}
    routing = ROUTING[member.egress]
    if routing:
        compat = {"openRouterRouting": dict(routing)}
        provider["compat"] = compat
        if member.model:
            provider["modelOverrides"] = {member.model: {"compat": compat}}
    return {"providers": {PROVIDER: provider}}


def local_models_config(models_file: Path | str, member: Member) -> dict[str, object]:
    """What ``models.json`` holds for the local *member*, from the operator's file.

    The provider is the operator's own: the one in *models_file* that holds
    the member's model under the ID the pool file check found it by, which may
    be one of its llama-swap aliases. Every field of it is copied but its model
    list, so a field pi learns later travels without this knowing of it, and
    the list holds that one model, copied whole.

    Raises ``LaunchError`` when the file cannot be read or holds no such model,
    naming the model and never the rest of what the file holds: a member that
    asked for something the operator does not have is a fault in the member's
    declaration, and the operator's other models are not the member's business.
    """
    try:
        data = json.loads(Path(models_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LaunchError(
            f"member '{member.name}': the operator's models file at {models_file} "
            f"could not be read ({exc})."
        ) from exc
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        raise LaunchError(
            f"member '{member.name}': the operator's models file at {models_file} "
            "declares no providers."
        )
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        models = provider.get("models")
        if not isinstance(models, list):
            continue
        for entry in models:
            if isinstance(entry, dict) and entry.get("id") == (member.pi_model or member.model):
                copied = {k: v for k, v in provider.items() if k != "models"}
                copied["models"] = [entry]
                return {"providers": {name: deepcopy(copied)}}
    raise LaunchError(
        f"member '{member.name}' declares the model '{member.model}', which the "
        f"operator's models file at {models_file} does not hold."
    )


def local_port(config_dir: Path | str, member: Member) -> int:
    """The loopback port the local *member*'s pi reaches its provider on.

    It is read from the provider's base URL in the member's generated models
    file, the one provider it holds. Inside the sandbox the member has
    loopback alone, and the forwarder to the bridge listens there on this
    port, so a base URL that is not plain HTTP on this host's IPv4 loopback
    would reach nothing and is refused naming it.
    """
    path = Path(config_dir) / MODELS_FILE
    try:
        providers = json.loads(path.read_text(encoding="utf-8"))["providers"]
        (provider,) = providers.values()
        base = str(provider["baseUrl"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LaunchError(
            f"member '{member.name}': its models file at {path} names no one provider "
            f"base URL ({exc})."
        ) from exc
    try:
        url = urlsplit(base)
        port = url.port or 80
    except ValueError:
        url = None
    if url is None or url.scheme != "http" or url.hostname not in _LOOPBACK:
        raise LaunchError(
            f"member '{member.name}' is local, and its provider's base URL {base} is not "
            "plain HTTP on 127.0.0.1 or localhost: inside its sandbox the member reaches "
            "llama-swap on loopback alone."
        )
    return port


def create(
    root: Path | str,
    member: Member,
    accounts: Iterable[Account],
    models_file: Path | str | None = None,
    *,
    skills: Iterable[str] = (),
    skills_from: Path | str | None = None,
    tools_from: Path | str | None = None,
) -> Path:
    """A new pi configuration directory for one lease of *member*, under *root*.

    Every member gets one, whatever its backing. Each call makes a directory of
    its own, open to this user alone, so ending one lease of a member that
    serves several leaves the others' in place.

    A hosted member's models file holds what its egress class asks of
    OpenRouter. A local member's holds the operator's own local provider,
    read from *models_file*, filtered to the model the member declares.

    Every directory gets a ``skills`` of its own, and each name in *skills*
    is copied into it from *skills_from*, dereferenced. The directory is
    made whatever the member declares: it says what the member's skills are,
    and for a member declaring none that is nothing at all.

    The git configuration file holds the operator's identity, as
    ``operator_identity`` reads it; with none, no file is written.

    Each of ``TOOLS`` that *tools_from* holds is copied into ``bin``, as a
    file of the member's own. Without it a local member's search tools fail,
    and a hosted member's fetch the program again for every lease.

    Raises ``LaunchError`` when a hosted member's account is not among
    *accounts*, when a local member's model is not in the operator's file or
    no *models_file* is given, and when a declared skill cannot be found or
    cannot be copied whole. Nothing is left behind in any of those cases.
    """
    sources = _skill_sources(member, skills, skills_from)
    if member.backing == HOSTED:
        config = models_config(member, member_account(member, tuple(accounts)))
    elif models_file is None:
        raise LaunchError(
            f"member '{member.name}' is local, so its models file is a copy of the "
            "operator's, and the pool was not told where that is. Declare it under "
            "'models_file' in the pool file."
        )
    else:
        config = local_models_config(models_file, member)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=f"{member.name}-", dir=root))
    try:
        if config is not None:
            # This user's alone from the moment it is made: a local member's
            # copy carries the operator's provider key as written.
            made = os.open(directory / MODELS_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(made, "w", encoding="utf-8") as file:
                file.write(json.dumps(config, indent=2) + "\n")
        _write_gitconfig(directory / GITCONFIG_FILE, operator_identity())
        _copy_skills(directory / SKILLS_DIR, sources, member)
        _copy_tools(directory / TOOLS_DIR, tools_from)
    except BaseException:
        # A directory half made is not handed on, and may hold the key already.
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory


def operator_identity() -> dict[str, str]:
    """The user.name and user.email this process's git reports outside any
    repository, by key; a key git has no value for is left out."""
    identity: dict[str, str] = {}
    for key in ("name", "email"):
        try:
            done = subprocess.run(
                ["git", "config", "--get", f"user.{key}"],
                cwd=os.path.abspath(os.sep), capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        value = done.stdout.removesuffix("\n")
        if done.returncode == 0 and value:
            identity[key] = value
    return identity


def _write_gitconfig(path: Path, identity: Mapping[str, str]) -> None:
    """Write *identity* as the user section of a git configuration file at
    *path*, this user's alone; with nothing in it, write nothing."""
    if not identity:
        return
    lines = ["[user]"] + [f"\t{key} = {_quoted(value)}" for key, value in identity.items()]
    made = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(made, "w", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")


def _quoted(value: str) -> str:
    """*value* as a quoted git configuration value, read back unchanged."""
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"')
        .replace("\n", "\\n").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _copy_tools(target: Path, tools_from: Path | str | None) -> None:
    """Copy each of ``TOOLS`` that *tools_from* holds into *target*, links followed."""
    if tools_from is None:
        return
    for name in TOOLS:
        source = Path(tools_from) / name
        if not source.is_file():
            continue
        target.mkdir(mode=0o700, exist_ok=True)
        shutil.copy(source, target / name)


def _skill_sources(
    member: Member, skills: Iterable[str], skills_from: Path | str | None
) -> list[tuple[str, Path]]:
    """Where each skill *member* declares is, in the order it was declared.

    A definition's skills are the member's whole set, because the sandbox
    hides every other place pi would find one, so a skill that cannot be
    copied in is one the member would run without. Such a member is refused
    here, before anything is made for its lease, and the message names the
    skill so the admin knows which line of the definition to fix.

    A name is one directory in the operator's skills directory: a name that
    walks out of it would copy whatever it reached, and is refused like a
    name that is not there at all.
    """
    source = None if skills_from is None else Path(skills_from)
    found = []
    for name in skills:
        if source is None:
            raise LaunchError(
                f"member '{member.name}' declares the skill '{name}', and the pool was "
                "not told where the operator's skills are."
            )
        held = source / name if name == Path(name).name else None
        if held is None or not held.is_dir():
            raise LaunchError(
                f"member '{member.name}' declares the skill '{name}', which is not a "
                f"directory in the operator's skills at {source}."
            )
        found.append((name, held))
    return found


def _copy_skills(into: Path, sources: Iterable[tuple[str, Path]], member: Member) -> None:
    """Copy each skill in *sources* into *into*, links resolved.

    The whole of a skill is copied, so a skill the operator keeps as a link
    into another checkout arrives as a real directory, and so does a file
    inside one. The copy is the member's own from then on: editing the
    source on the host leaves a running member's skill as it was. A skill
    that cannot be copied whole is refused by name, as one that is not there
    is: the member would run without part of it.
    """
    into.mkdir(parents=True, exist_ok=True)
    for name, held in sources:
        try:
            shutil.copytree(held, into / name, symlinks=False)
        except (shutil.Error, OSError):
            raise LaunchError(
                f"member '{member.name}' declares the skill '{name}', which cannot be "
                "copied whole: a file in it cannot be read, or is a link to nothing."
            ) from None


def remove(directory: Path | str | None, root: Path | str) -> None:
    """Remove a generated *directory* and all pi wrote there; the lease has ended.

    Every member has one now, a local member included, so every lease ends
    with one to remove. Nothing at all, and a directory already gone, are left
    alone, so ending a lease twice is harmless.

    What is removed is decided by where *directory* is and never by what is in
    it: the member writes in its directory and may take anything out of it.
    Raises ``LaunchError`` for a path that is not a direct child of the
    configuration *root*, the one directory every generated directory is made
    in: what reaches this by mistake may be the operator's own configuration.
    """
    if directory is None:
        return
    directory = Path(os.path.abspath(directory))
    if directory.parent != Path(os.path.abspath(root)):
        raise LaunchError(
            f"{directory} is not a generated pi configuration directory, a direct child of "
            f"{root}; it is not removed."
        )
    if not os.path.lexists(directory):
        return
    if directory.is_symlink() or not directory.is_dir():
        raise LaunchError(
            f"{directory} is not a generated pi configuration directory; it is not removed."
        )
    try:
        shutil.rmtree(directory)
    except OSError:
        # The member may write here, and a directory it took its own rights
        # from is still this user's to open again.
        _reopen(directory)
        shutil.rmtree(directory)


def _reopen(directory: Path) -> None:
    """Give this user back every right on each directory under *directory*,
    from the top down, following no link."""
    left = [directory]
    while left:
        here = left.pop()
        os.chmod(here, 0o700)
        with os.scandir(here) as entries:
            left.extend(entry.path for entry in entries if entry.is_dir(follow_symlinks=False))
