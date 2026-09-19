"""A hosted member's pi configuration directory: where its egress class takes effect.

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

pi reads ``models.json`` from its configuration directory, so each lease of a
hosted member gets a directory of its own, generated here, and the member's
environment points pi at it. A ``no-train`` member's file carries both flags; an
``open`` member's carries neither. A local member sends nothing off this host
and nothing is generated for it: it runs against the user's own pi
configuration, which is where its llama-swap provider is declared.

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
from collections.abc import Iterable
from pathlib import Path

from .config import HOSTED, Account, Member
from .launch import LaunchError, member_account

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

# Written into every generated directory, and the only thing that lets one be
# removed: a path that reaches remove() by mistake may be the user's own pi
# directory.
MARKER = ".specflo-pool-member"


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


def create(root: Path | str, member: Member, accounts: Iterable[Account]) -> Path | None:
    """A new pi configuration directory for one lease of *member*, under *root*.

    Returns None for a member that is not hosted, and writes nothing. Each call
    makes a directory of its own, open to this user alone, so ending one lease
    of a member that serves several leaves the others' in place.

    Raises ``LaunchError`` when the member's account is not among *accounts*.
    """
    if member.backing != HOSTED:
        return None
    config = models_config(member, member_account(member, tuple(accounts)))
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=f"{member.name}-", dir=root))
    (directory / MARKER).write_text("", encoding="utf-8")
    (directory / MODELS_FILE).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return directory


def remove(directory: Path | str | None) -> None:
    """Remove a generated *directory* and all pi wrote there; the lease has ended.

    None, the answer for a local member, and a directory already gone are left
    alone, so ending a lease twice is harmless.

    Raises ``LaunchError`` for a directory that was not generated here.
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
