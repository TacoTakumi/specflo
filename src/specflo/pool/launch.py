"""Starting a member: a definition becomes pi startup flags and a scoped environment.

A definition's tools and prompt reach pi by one route only, the command line:
its tools go to ``--tools`` and the prompt body to ``--append-system-prompt``.
The project's AGENTS.md and CLAUDE.md are kept out with ``--no-context-files``
unless the definition asks for them, so a project cannot silently rewrite a
member's role.

A definition's skills do not travel as a flag. ``--skill`` is a path with no
resolver, and the operator's skills are hidden from the member anyway, so each
declared skill is copied into the member's generated configuration directory
instead (see ``piconfig``), where pi discovers it by name.

What a member is given is the only hard limit on it, so its environment is
built from nothing: the baseline below, the variables and credentials its
definition lists, and the API key of its own account for a hosted member.
Nothing else in the caller's environment reaches it.

A definition's command deny list is the one part that does not travel as a
flag. Every member loads the pool's deny-list extension with ``-e``, and the
list reaches it in one environment variable. That extension is a guard against
mistakes, not a limit: see its header.

A member's egress class does not travel as a flag either. It is written into a
pi configuration directory generated for the member (see ``piconfig``), and one
environment variable points the member's pi at that directory. Every member
gets one and no member starts without it: the sandbox hides the operator's own
configuration, so a member whose pi was not pointed somewhere of its own would
start against a directory that is not there.

None of that is a boundary by itself: a flag is a request to pi, and what
pi loads is decided by the machine it runs on. The boundary is the sandbox
the member starts inside, and ``member_argv`` is where the two meet - the
sandbox prefix in front, the pi command line behind it. Every member start
goes through that one function, so there is no second way to start a member
with the flags and without the boundary.

pi loads the extensions a developer has installed, in a member as anywhere
else, and one of them is specflo's own control extension. A member is reached
through its agent host, whose socket is where the lease wall stands, so the
member's environment tells that extension to serve nothing of its own;
otherwise it binds a second socket, named for the working directory, that
answers for the member with no lease behind it.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterable, Mapping
from pathlib import Path

from ..errors import SpecfloError
from . import sandbox
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

# The variable that tells pi where its configuration directory is. It is set
# only to a directory generated for the member, never from the caller: the
# caller's own pi configuration carries no routing flags.
AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"

# What a member says to the specflo control extension, which pi discovers
# wherever a developer has installed it and loads inside a member too. The
# member is driven through its agent host, and the host's socket is where the
# lease wall stands, so the extension must serve nothing of its own: a second
# socket would answer for the member with no lease behind it. The other two
# are the handshake the extension reads when it does serve, set here so the
# member is identified by its own name wherever the extension uses one.
SERVE_ENV = "SPECFLO_AGENT_SERVE"
AGENT_NAME_ENV = "SPECFLO_AGENT_NAME"
AGENT_MANAGED_ENV = "SPECFLO_AGENT_MANAGED"

# The variables the pool alone says, whatever a definition lists or a caller
# exports. A member that could set one of these could give itself a name, a
# deny list or a pi configuration of its own choosing.
_POOL_SAYS: tuple[str, ...] = (
    DENY_ENV, AGENT_DIR_ENV, SERVE_ENV, AGENT_NAME_ENV, AGENT_MANAGED_ENV,
)


class LaunchError(SpecfloError):
    """A member that cannot be started as configured. Names variables, never their values."""


def pi_argv(definition: AgentDefinition, member: Member) -> list[str]:
    """The command line that starts *member* in the role *definition* gives.

    The member's harness command comes first, split the way the agent host
    splits its pi command. The definition's skills are not here: they are in
    the member's generated configuration directory, where pi finds them by
    name. Nothing is read from or written to the environment.
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
    argv += ["--append-system-prompt", definition.prompt]
    if not definition.project_context:
        argv.append("--no-context-files")
    return argv


def member_argv(
    definition: AgentDefinition,
    member: Member,
    environ: Mapping[str, str],
    *,
    cwd: Path | str,
    state_dir: Path | str,
    config_dir: Path | str | None = None,
    limits: sandbox.Limits | None = None,
) -> list[str]:
    """The whole of what *member* starts as: its sandbox, then its pi.

    The sandbox comes from the member's own egress class, and a class with no
    profile raises here rather than starting the member without one.

    What the member gets back from under the swept home is the least that
    lets it run: the harness's own installation, the pool's deny-list
    extension, which ships inside this package and is read by the member's
    pi, and its working directory and generated directory, the two it may
    write. *environ* is the daemon's environment, read for the operator's
    paths and for the PATH the harness is looked up on; nothing is written
    to it.
    """
    hidden = sandbox.operator_paths(environ)
    program = shlex.split(member.command)[0]
    writable = [str(cwd), *([str(config_dir)] if config_dir is not None else [])]
    readonly = [
        *sandbox.harness_paths(program, environ),
        str(Path(DENY_EXTENSION).resolve().parent),
    ]
    prefix = sandbox.base_argv(
        egress=member.egress,
        hidden=hidden,
        empty=sandbox.empty_file(state_dir),
        writable=writable,
        readonly=readonly,
        limits=sandbox.DEFAULT_LIMITS if limits is None else limits,
        chdir=cwd,
    )
    return [*prefix, *pi_argv(definition, member)]


def member_env(
    definition: AgentDefinition,
    member: Member,
    accounts: Iterable[Account],
    environ: Mapping[str, str],
    *,
    config_dir: Path | None = None,
) -> dict[str, str]:
    """The environment *member* starts with in the role *definition* gives.

    Values come from *environ*, the caller's environment, which is read and
    never changed; a name it does not hold is left out. No account's key
    variable is handed over because a definition lists it: an account's cap
    holds only if its key reaches its own members alone. The deny list comes
    from the definition alone, never from the caller; a definition that denies
    nothing sets no variable. *config_dir* is the pi configuration directory
    generated for the member, and nothing is written here.

    Raises ``LaunchError`` for a hosted member whose account is not among
    *accounts* or whose key variable is not set, and for any member handed no
    *config_dir*: every member has one, and a member without it would start
    against a directory the sandbox hides.
    """
    accounts = tuple(accounts)
    key_vars = {account.key_env for account in accounts}
    allowed = [
        name
        for name in (*BASELINE_ENV, *definition.env, *definition.credentials)
        if name not in key_vars and name not in _POOL_SAYS
    ]
    if member.account is not None:
        allowed.append(_key_var(member, accounts, environ))
    env = {name: environ[name] for name in allowed if name in environ}
    if definition.deny:
        env[DENY_ENV] = json.dumps(list(definition.deny))
    if config_dir is None:
        raise LaunchError(
            f"member '{member.name}' has no generated pi configuration directory, so "
            f"{AGENT_DIR_ENV} cannot be set and it would start against a directory "
            "the sandbox hides."
        )
    env[AGENT_DIR_ENV] = str(config_dir)
    env[SERVE_ENV] = "0"
    env[AGENT_NAME_ENV] = member.name
    env[AGENT_MANAGED_ENV] = "1"
    return env


def member_account(member: Member, accounts: Iterable[Account]) -> Account:
    """The declared account the hosted *member* runs through.

    Raises ``LaunchError`` when it is not among *accounts*.
    """
    account = next((a for a in accounts if a.name == member.account), None)
    if account is None:
        raise LaunchError(
            f"member '{member.name}': account '{member.account}' is not a declared account."
        )
    return account


def _key_var(member: Member, accounts: tuple[Account, ...], environ: Mapping[str, str]) -> str:
    """The name of the variable that holds the key of *member*'s account."""
    account = member_account(member, accounts)
    if not environ.get(account.key_env):
        raise LaunchError(
            f"member '{member.name}': the API key of account '{account.name}' is not set; "
            f"export {account.key_env} where the pool runs."
        )
    return account.key_env
