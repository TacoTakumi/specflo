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

import dataclasses
import json
import os
import shlex
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path

from ..config import CONFIG_DIRNAME, CONFIG_FILENAME, REMOTES_DIRNAME
from ..errors import SpecfloError
from . import sandbox
from .config import LOCAL, Account, Member
from .definitions import AgentDefinition

# The variables every member gets, and the whole of what it gets unasked. pi is
# a node program started through "env node", so it needs PATH to be found and
# HOME for its settings, models and sessions; the rest are what a program
# expects of any login: who it runs as, the shell its bash tool starts, the
# locale, the time zone and the terminal type. None of them holds a secret.
# The temp directory is the pool's to say: the sandbox's /tmp is its own. Proxy settings, editor choices and anything a version
# manager exports are not here; a definition that needs one lists it.
BASELINE_ENV: tuple[str, ...] = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
)

# The pi extension that enforces a definition's deny list. It ships inside
# the package, so the path holds for a checkout and for an installed wheel.
DENY_EXTENSION = str(Path(__file__).resolve().parent / "pi_extension" / "deny.ts")

# The variable the extension reads the deny list from, as a JSON array of
# strings. The name is fixed on both sides; deny.ts holds the other copy.
DENY_ENV = "SPECFLO_POOL_DENY"

# Where a checkout keeps the lease tokens its holder was granted, under its
# .specflo directory. The name is fixed on both sides: the agent subsystem's
# lease module holds the other copy, and this module does not import it.
TOKEN_DIRNAME = "leases"

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

# Where a member keeps its temporary files: the sandbox's fresh /tmp, since a
# temp directory the daemon names elsewhere is not there inside.
TMPDIR_ENV = "TMPDIR"
MEMBER_TMPDIR = "/tmp"

# The git global configuration a member reads: a file in its generated
# directory holding the operator's identity alone. The home the sandbox sweeps
# has none, and git refuses to commit without one.
GIT_CONFIG_GLOBAL_ENV = "GIT_CONFIG_GLOBAL"
GITCONFIG_FILE = "gitconfig"

# The variables the pool alone says, whatever a definition lists or a caller
# exports. A member that could set one of these could give itself a name, a
# deny list or a pi configuration of its own choosing.
_POOL_SAYS: tuple[str, ...] = (
    DENY_ENV, AGENT_DIR_ENV, SERVE_ENV, AGENT_NAME_ENV, AGENT_MANAGED_ENV, TMPDIR_ENV,
    GIT_CONFIG_GLOBAL_ENV,
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
    bridge: sandbox.Bridge | None = None,
    daemon_root: Path | str | None = None,
) -> list[str]:
    """The whole of what *member* starts as: its sandbox, then its pi.

    The sandbox comes from the member's own egress class, and a class with no
    profile raises here rather than starting the member without one.

    Besides the operator's paths, the sandbox hides the secrets a checkout
    keeps beside the working directory (``checkout_secrets``) and
    *daemon_root*, the daemon's own directory, which holds the pool's token
    and every client's. The member's generated directory and the bridge
    socket are under that root, and come back as any bind does.

    What the member gets back from under the swept home is the least that
    lets it run: the harness's own installation, the pool's deny-list
    extension, which ships inside this package and is read by the member's
    pi, and its working directory and generated directory, the two it may
    write. *environ* is the daemon's environment, read for the operator's
    paths and for the PATH the harness is looked up on; nothing is written
    to it.

    A local member with a *bridge* gets the bridge socket bound in besides,
    and starts as the launcher that brings the forwarder up before it
    becomes pi. A bridge is refused for any other member: one that shares
    the host's network has no use for it, and would have a second way to
    llama-swap.

    The working directory, the generated directory and the bridge socket are
    taken at their real paths. The hidden paths are real paths, and a bind at
    a link's own path would bring a checkout back under a name none of them
    covers.
    """
    fault = working_directory_fault(cwd, environ, daemon_root)
    if fault is not None:
        raise LaunchError(f"member '{member.name}' cannot start: {fault}")
    cwd = os.path.realpath(cwd)
    if config_dir is not None:
        config_dir = os.path.realpath(config_dir)
    if bridge is not None:
        bridge = dataclasses.replace(bridge, socket=os.path.realpath(bridge.socket))
    hidden = [*_swept(environ, daemon_root), *checkout_secrets(cwd)]
    program = shlex.split(member.command)[0]
    writable = [str(cwd), *([str(config_dir)] if config_dir is not None else [])]
    readonly = [
        *sandbox.harness_paths(program, environ),
        str(Path(DENY_EXTENSION).resolve().parent),
    ]
    for path in readonly:
        covered = _covered(path, hidden)
        if covered is not None:
            raise LaunchError(
                f"member '{member.name}' cannot start: its harness '{program}' is read from "
                f"'{path}', which holds '{covered}', a directory the sandbox hides from every "
                "member, and a read-only bind would bring it back. Install the harness under "
                "a directory below the home that holds none of those."
            )
    listed, links = _listed_binds(definition, member, environ, daemon_root, hidden)
    readonly.extend(listed)
    forwarder: list[str] = []
    if bridge is not None:
        if member.backing != LOCAL:
            raise LaunchError(
                f"member '{member.name}' is {member.backing}, and only a local member "
                "reaches its model through the bridge."
            )
        socat = shutil.which("socat", path=environ.get("PATH"))
        if socat is None:
            raise LaunchError(
                f"member '{member.name}' is local and reaches its model through a "
                "forwarder inside its sandbox, and socat, which the forwarder is, is not "
                "on the daemon's PATH."
            )
        readonly.append(bridge.socket)
        forwarder = sandbox.forwarder_argv(bridge, socat)
    # A member's process count is its scope's own when the user manager can
    # make one; the per-uid count prlimit sets counts the whole host.
    scope, limits = sandbox.member_limits(
        sandbox.DEFAULT_LIMITS if limits is None else limits, environ, member.name
    )
    prefix = sandbox.base_argv(
        egress=member.egress,
        hidden=hidden,
        empty=sandbox.empty_file(state_dir),
        writable=writable,
        readonly=readonly,
        links=links,
        limits=limits,
        chdir=cwd,
    )
    return [*scope, *prefix, *forwarder, *pi_argv(definition, member)]


def _listed_binds(
    definition: AgentDefinition,
    member: Member,
    environ: Mapping[str, str],
    daemon_root: Path | str | None,
    hidden: list[str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """The real paths of the paths *definition* lists, to bind read-only, and
    the links on the way to each that lie in a swept directory, as (target,
    path) pairs to make again.

    Each listed path is checked again here, as validation checked it: it may
    have gone, or become a link to a secret, since. Raises ``LaunchError``
    naming the path when it is at fault, or when it covers one of *hidden*.
    """
    real: list[str] = []
    links: list[tuple[str, str]] = []
    for listed in definition.paths:
        fault = listed_path_fault(listed, environ, daemon_root)
        given = listed_path(listed, environ)
        covered = _covered(given, hidden) if fault is None and given is not None else None
        if covered is not None:
            fault = (
                f"'{listed}' holds '{covered}', a directory the sandbox hides from every "
                "member, and a bind would bring it back."
            )
        if fault is not None:
            raise LaunchError(
                f"member '{member.name}' cannot start: definition '{definition.name}' "
                f"lists a path it may not have: {fault}"
            )
        chain = sandbox.link_chain(os.path.abspath(given))
        for step in chain[:-1]:
            swept = any(os.path.commonpath([step, held]) == held for held in hidden)
            if swept and os.path.islink(step):
                links.append((os.readlink(step), step))
        real.append(os.path.realpath(given))
    return real, links


def working_directory_fault(
    cwd: Path | str, environ: Mapping[str, str], daemon_root: Path | str | None = None
) -> str | None:
    """Why a member may not work in *cwd*, in one sentence, or None when it may.

    A member's working directory is bound in writable after the directories
    lying above a bind are hidden, so it brings back whatever of them it
    covers. A working directory at or above a hidden path brings all of it
    back: with the home, the member reads the operator's keys and writes the
    shell files the operator runs next. One inside a hidden path brings back
    that part of it. The home is the one hidden path a member may work
    inside, since the sandbox is built for exactly that.

    A checkout's token directories are hidden too, and a working directory
    inside any ``.specflo`` directory is refused for them. *environ* is the
    daemon's environment and *daemon_root* its own directory, as for
    ``member_argv``.
    """
    here = os.path.realpath(cwd)
    swept = _swept(environ, daemon_root)
    home = swept[0]
    for path in swept:
        if os.path.commonpath([path, here]) == here:
            what = "is the operator's home or holds it" if path == home else (
                "holds a directory the sandbox hides from every member"
            )
            return (
                f"the working directory '{cwd}' {what}, and a working directory is bound in "
                "writable over what it holds, so the member would have it back. Start the "
                "member in a directory below the home that holds none of those."
            )
        if path != home and os.path.commonpath([path, here]) == path:
            return (
                f"the working directory '{cwd}' is inside a directory the sandbox hides from "
                "every member, and the member would have that part of it back."
            )
    if CONFIG_DIRNAME in Path(here).parts:
        return (
            f"the working directory '{cwd}' is inside a {CONFIG_DIRNAME} directory, which "
            "holds lease and daemon tokens the sandbox hides from every member."
        )
    return None


def listed_path(listed: str, environ: Mapping[str, str]) -> str | None:
    """The absolute path a definition's *listed* path names, a leading ``~``
    taken as the home in *environ*; None for a path that is neither."""
    if listed == "~" or listed.startswith("~/"):
        home = environ.get("HOME") or str(Path.home())
        return os.path.join(home, listed[2:]) if listed != "~" else home
    return listed if os.path.isabs(listed) else None


def listed_path_fault(
    listed: str, environ: Mapping[str, str], daemon_root: Path | str | None = None
) -> str | None:
    """Why a definition's *listed* path may not be bound into a member's
    sandbox, in one sentence naming it, or None when it may.

    A listed path is bound read-only after the hidden paths are, so it brings
    back whatever of them it covers. One at or above a hidden path, or at or
    below one other than the home, is refused, by its given and by its real
    path, and so is one in or holding a checkout's ``.specflo`` directory,
    which holds lease and daemon tokens. So is a path that does not exist,
    and one that is neither absolute nor under ``~``. *environ* is the
    daemon's environment and *daemon_root* its own directory, as for
    ``member_argv``.
    """
    given = listed_path(listed, environ)
    if given is None:
        return f"'{listed}' is neither an absolute path nor one under ~; write it as either."
    if not os.path.exists(given):
        return f"'{listed}' does not exist, so there is nothing to bind."
    swept = _swept(environ, daemon_root)
    home = swept[0]
    forms = {os.path.abspath(given), os.path.realpath(given)}
    for form in forms:
        for held in swept:
            if os.path.commonpath([form, held]) == form:
                return (
                    f"'{listed}' holds '{held}', a directory the sandbox hides from every "
                    "member, and a bind would bring it back."
                )
            if held != home and os.path.commonpath([form, held]) == held:
                return (
                    f"'{listed}' is inside '{held}', a directory the sandbox hides from every "
                    "member, and a bind would bring that part of it back."
                )
        if CONFIG_DIRNAME in Path(form).parts or os.path.isdir(os.path.join(form, CONFIG_DIRNAME)):
            return (
                f"'{listed}' is or holds a {CONFIG_DIRNAME} directory, which holds lease and "
                "daemon tokens the sandbox hides from every member."
            )
    return None


def _covered(path: str, hidden: Iterable[str]) -> str | None:
    """The first of *hidden* that *path*, by its given or its real path, is at
    or above, or None when it covers none of them."""
    forms = {os.path.abspath(path), os.path.realpath(path)}
    for held in hidden:
        if any(os.path.commonpath([form, held]) == form for form in forms):
            return held
    return None


def _swept(environ: Mapping[str, str], daemon_root: Path | str | None) -> list[str]:
    """The operator's paths, the home first, and the daemon root after them."""
    return [
        *sandbox.operator_paths(environ),
        *([str(Path(daemon_root).resolve())] if daemon_root is not None else []),
    ]


def checkout_secrets(cwd: Path | str) -> tuple[str, ...]:
    """The directories of secrets kept by every checkout *cwd* is in, made if need be.

    A checkout keeps each lease token its holder was granted in
    ``.specflo/leases`` and the bearer token of each daemon it reaches in
    ``.specflo/remotes``. A member usually works in the checkout it was leased
    from, or below it, and the agent verbs look for a token from the working
    directory upward, so every checkout from *cwd* up to the root is looked
    at.

    A directory is a checkout when its ``.specflo`` folder holds the
    configuration file, the test the commands use to find one. Both
    directories are made (this user's alone) in a checkout that does not have
    them yet: a sandbox does not hide a path that is not there, and the token
    of a lease is written once its member has started. The commands that
    write to them leave a directory that is there already as it is.

    A ``.specflo`` folder with no configuration file, such as the agent state
    directory in the home, is no checkout, and nothing is made in it; a
    token directory already there is hidden all the same.
    """
    here = Path(os.path.abspath(cwd))
    found: list[str] = []
    for directory in (here, *here.parents):
        folder = directory / CONFIG_DIRNAME
        if not folder.is_dir():
            continue
        checkout = (folder / CONFIG_FILENAME).is_file()
        for held in (folder / TOKEN_DIRNAME, folder / REMOTES_DIRNAME):
            if checkout:
                try:
                    held.mkdir(mode=0o700, exist_ok=True)
                except OSError:
                    # One this user cannot make is one no token is written to.
                    continue
            elif not held.is_dir():
                continue
            found.append(str(held.resolve()))
    return tuple(found)


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
    generated for the member, and nothing is written here; git is pointed at
    the configuration file in it when there is one.

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
    env[TMPDIR_ENV] = MEMBER_TMPDIR
    gitconfig = Path(config_dir) / GITCONFIG_FILE
    if gitconfig.is_file():
        env[GIT_CONFIG_GLOBAL_ENV] = str(gitconfig)
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
