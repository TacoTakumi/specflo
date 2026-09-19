"""Starting a member: a definition becomes pi startup flags and a scoped environment.

A definition's tools, skills and prompt reach pi by one route only, the command
line: its tools go to ``--tools``, each skill to ``--skill`` and the prompt body
to ``--append-system-prompt``. The project's AGENTS.md and CLAUDE.md are kept out
with ``--no-context-files`` unless the definition asks for them, so a project
cannot silently rewrite a member's role.

What a member is given is the only hard limit on it, so its environment is
built from nothing: the baseline below, the variables and credentials its
definition lists, and the API key of its own account for a hosted member.
Nothing else in the caller's environment reaches it.

A definition's command deny list is the one part that does not travel as a
flag. Every member loads the pool's deny-list extension with ``-e``, and the
list reaches it in one environment variable. That extension is a guard against
mistakes, not a limit: see its header.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterable, Mapping
from pathlib import Path

from ..errors import SpecfloError
from .config import Account, Member
from .definitions import AgentDefinition

# The variables every member gets, and the whole of what it gets unasked. pi is
# a node program started through "env node", so it needs PATH to be found and
# HOME for its settings, models and sessions; the rest are what a program
# expects of any login: who it runs as, the shell its bash tool starts, the
# locale, the time zone, the terminal type and the temp directory. None of
# them holds a secret. Proxy settings, editor choices and anything a version
# manager exports are not here; a definition that needs one lists it.
BASELINE_ENV: tuple[str, ...] = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "TMPDIR",
)

# The pi extension that enforces a definition's deny list. It ships inside
# the package, so the path holds for a checkout and for an installed wheel.
DENY_EXTENSION = str(Path(__file__).resolve().parent / "pi_extension" / "deny.ts")

# The variable the extension reads the deny list from, as a JSON array of
# strings. The name is fixed on both sides; deny.ts holds the other copy.
DENY_ENV = "SPECFLO_POOL_DENY"


class LaunchError(SpecfloError):
    """A member that cannot be started as configured. Names variables, never their values."""


def pi_argv(definition: AgentDefinition, member: Member) -> list[str]:
    """The command line that starts *member* in the role *definition* gives.

    The member's harness command comes first, split the way the agent host
    splits its pi command. Nothing is read from or written to the environment.
    """
    argv = shlex.split(member.command)
    # On every member, whatever its definition denies: the deny list itself
    # travels in the environment, so one command line shape fits all.
    argv += ["-e", DENY_EXTENSION]
    if definition.tools:
        argv += ["--tools", ",".join(definition.tools)]
    else:
        # With no --tools pi enables its default tools, which is more than a
        # definition that lists none allows.
        argv.append("--no-tools")
    for skill in definition.skills:
        argv += ["--skill", skill]
    argv += ["--append-system-prompt", definition.prompt]
    if not definition.project_context:
        argv.append("--no-context-files")
    return argv


def member_env(
    definition: AgentDefinition,
    member: Member,
    accounts: Iterable[Account],
    environ: Mapping[str, str],
) -> dict[str, str]:
    """The environment *member* starts with in the role *definition* gives.

    Values come from *environ*, the caller's environment, which is read and
    never changed; a name it does not hold is left out. No account's key
    variable is handed over because a definition lists it: an account's cap
    holds only if its key reaches its own members alone. The deny list comes
    from the definition alone, never from the caller; a definition that denies
    nothing sets no variable.

    Raises ``LaunchError`` for a hosted member whose account is not among
    *accounts* or whose key variable is not set.
    """
    accounts = tuple(accounts)
    key_vars = {account.key_env for account in accounts}
    allowed = [
        name
        for name in (*BASELINE_ENV, *definition.env, *definition.credentials)
        if name not in key_vars and name != DENY_ENV
    ]
    if member.account is not None:
        allowed.append(_key_var(member, accounts, environ))
    env = {name: environ[name] for name in allowed if name in environ}
    if definition.deny:
        env[DENY_ENV] = json.dumps(list(definition.deny))
    return env


def _key_var(member: Member, accounts: tuple[Account, ...], environ: Mapping[str, str]) -> str:
    """The name of the variable that holds the key of *member*'s account."""
    account = next((a for a in accounts if a.name == member.account), None)
    if account is None:
        raise LaunchError(
            f"member '{member.name}': account '{member.account}' is not a declared account."
        )
    if not environ.get(account.key_env):
        raise LaunchError(
            f"member '{member.name}': the API key of account '{account.name}' is not set; "
            f"export {account.key_env} where the pool runs."
        )
    return account.key_env
