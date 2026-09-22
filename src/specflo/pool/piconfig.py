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
"""

from __future__ import annotations

import json
import shutil
import tempfile
from copy import deepcopy
from collections.abc import Iterable, Mapping
from pathlib import Path

from .config import HOSTED, Account, Member
from .launch import AGENT_DIR_ENV, LaunchError, member_account

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

# Where the operator's pi configuration directory is when the environment
# names none.
_DEFAULT_AGENT_DIR = Path(".pi") / "agent"

# Written into every generated directory, and the only thing that lets one be
# removed: a path that reaches remove() by mistake may be the user's own pi
# directory.
MARKER = ".specflo-pool-member"


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

    The provider is the operator's own: the one in *models_file* that holds a
    model whose id is the one the member declares. Every field of it is copied
    but its model list, so a field pi learns later travels without this knowing
    of it, and the list holds that one model, copied whole.

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
            if isinstance(entry, dict) and entry.get("id") == member.model:
                copied = {k: v for k, v in provider.items() if k != "models"}
                copied["models"] = [entry]
                return {"providers": {name: deepcopy(copied)}}
    raise LaunchError(
        f"member '{member.name}' declares the model '{member.model}', which the "
        f"operator's models file at {models_file} does not hold."
    )


def create(
    root: Path | str,
    member: Member,
    accounts: Iterable[Account],
    models_file: Path | str | None = None,
    *,
    skills: Iterable[str] = (),
    skills_from: Path | str | None = None,
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

    Raises ``LaunchError`` when a hosted member's account is not among
    *accounts*, and when a local member's model is not in the operator's file
    or no *models_file* is given. Nothing is made in either case.
    """
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
    (directory / MARKER).write_text("", encoding="utf-8")
    if config is not None:
        (directory / MODELS_FILE).write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )
    _copy_skills(directory / SKILLS_DIR, skills, skills_from)
    return directory


def _copy_skills(
    into: Path, skills: Iterable[str], skills_from: Path | str | None
) -> None:
    """Copy each named skill from *skills_from* into *into*, links resolved.

    The whole of a skill is copied, so a skill the operator keeps as a link
    into another checkout arrives as a real directory, and so does a file
    inside one. The copy is the member's own from then on: editing the
    source on the host leaves a running member's skill as it was.
    """
    into.mkdir(parents=True, exist_ok=True)
    if skills_from is None:
        return
    source = Path(skills_from)
    for name in skills:
        held = source / name
        if held.is_dir():
            shutil.copytree(held, into / name, symlinks=False)


def remove(directory: Path | str | None) -> None:
    """Remove a generated *directory* and all pi wrote there; the lease has ended.

    Every member has one now, a local member included, so every lease ends
    with one to remove. Nothing at all, and a directory already gone, are left
    alone, so ending a lease twice is harmless.

    Raises ``LaunchError`` for a directory that was not generated here: what
    reaches this by mistake may be the operator's own configuration, and the
    marker is the only thing that tells one from the other.
    """
    if directory is None:
        return
    directory = Path(directory)
    if not directory.exists():
        return
    if not (directory / MARKER).is_file():
        raise LaunchError(
            f"{directory} is not a generated pi configuration directory; it is not removed."
        )
    shutil.rmtree(directory)
