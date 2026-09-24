"""The bubblewrap sandbox a member runs inside: the argv the daemon starts it behind.

A member's egress class is a claim about where what it is working on can go,
and until now it was a claim about a list: the environment the pool handed
over, the flags on the command line. What the member actually loaded was
decided by the machine, so the claim held only as far as the list did. The
sandbox makes the class a property of the namespaces the process runs in,
which no extension, skill or subprocess of the member can argue with.

This module builds the argv and starts nothing. It reads no configuration and
asks the caller for every path it mounts, so the same rig gives the same argv
every time and a test can read the whole boundary without making one. What it
does touch of the host is presence - whether a path to hide is there, and a
directory or a file - and the one empty file a hidden file is covered with,
which it makes if it is not there yet. The caller appends the member's own
command after the separator the prefix ends with.

Which sandbox a member gets is decided by its egress class, and there is one
profile for each class the pool declares. A class with no profile is refused
by name rather than answered with a sandbox nobody chose.

Later mounts overlay earlier ones, so the order is the boundary:

1. the namespace and process-safety flags, and the profile's answer to them
2. the read-only root, then a fresh dev, proc and temp over it
3. the hidden paths that lie above a writable bind, the home most of all:
   a member's working directory is under the operator's home, so the home
   is swept first and the working directory bound back on top of it
4. the read-only binds: what a member needs back from under the swept
   home and cannot do without, the harness it runs as above all - pi is
   installed under the operator's home on this rig, so a swept home with
   nothing bound back leaves nothing to start
5. the writable binds - the member's own working directory and the
   directory generated for its lease - and the pins that hold the hidden
   set up inside them
6. the rest of the hidden set: a tmpfs over each of the operator's
   directories and an empty file over each of its files. These come last,
   so a bind of either kind cannot bring back what one of them covers
7. the working directory the member starts in

A fresh ``/tmp`` matters more than it looks: it is the one directory every
tool writes to, so a member sharing the host's would read what the operator's
own work left there and leave its own behind for the next lease.

The resource limits are a program in front of all of it. prlimit(1) sets
them on itself and execs bwrap, so they are in place before the sandbox is
made and nothing of the daemon's own runs between the fork and the exec: the
daemon is threaded, and a pre-exec callback in a threaded process is
documented as unsafe.

The sandbox is only as good as the kernel and the policy under it. On a rig
where an unprivileged user namespace cannot be made, bwrap refuses and there
is no boundary to have; ``unavailable`` says so in one line, for a test to
skip on and for a start to refuse with.
"""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..errors import SpecfloError

# The operator's directories, relative to the operator's home. Each holds
# something a member must not have: the stored provider keys and the whole
# configuration pi loads, the skills pi reads from the home whatever its
# configuration directory says, and the pool's own state - the lease token
# files a member could present as another holder, and the agent sockets it
# could drive another member through.
_UNDER_HOME = (".pi", ".agents", ".specflo")

# The variable naming the pi configuration directory. The daemon's own value
# is the operator's directory; a member's is the one generated for its lease,
# which is bound back over the hidden set and is not read here.
_AGENT_DIR_VAR = "PI_CODING_AGENT_DIR"

# The name the empty file is made under. A file is hidden by binding this
# over it: bwrap turns a tmpfs on a file down, and a bind of /dev/null mounts
# but reads back EACCES, because bwrap remounts a bind nodev inside the user
# namespace. An empty regular file is the one thing that reads as nothing.
_EMPTY_NAME = ".empty"

# What the sandbox itself forks before the member's own command runs: bwrap,
# the init it puts at pid 1 inside, the shell the command arrives as and what
# that shell starts. The process limit stands above these, so a member's
# allowance is what the member may add.
_SANDBOX_PROCESSES = 16

# The prlimit flag each limit is set through.
_LIMIT_FLAGS: dict[int, str] = {
    resource.RLIMIT_AS: "--as",
    resource.RLIMIT_CPU: "--cpu",
    resource.RLIMIT_NPROC: "--nproc",
}

# What the probe runs: the smallest sandbox bwrap can be asked for.
_PROBE = ("--unshare-user", "--ro-bind", "/", "/", "true")

# How long the probe may take. It makes a namespace and runs true, so a rig
# that has not answered by now is not going to.
_PROBE_TIMEOUT = 10.0


def bwrap_path() -> str | None:
    """Where bwrap is on this rig, or None when it is not on PATH."""
    return shutil.which("bwrap")


def unavailable() -> str | None:
    """Why this rig cannot make a sandbox, in one line, or None when it can.

    The answer is a fact about the rig - bwrap installed, unprivileged user
    namespaces permitted by the kernel and by whatever policy stands over it -
    so it is found by making the smallest sandbox there is rather than by
    reading a setting. A rig that says no here says no to every member.
    """
    path = bwrap_path()
    if path is None:
        return "bwrap is not on PATH"
    try:
        # The program is named in the argv and the resolved path is passed
        # beside it, so what this module starts can be read from the source.
        probe = subprocess.run(
            ["bwrap", *_PROBE],
            executable=path,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except OSError as error:
        return f"bwrap could not be run: {error}"
    except subprocess.TimeoutExpired:
        return "bwrap did not answer the user-namespace probe"
    if probe.returncode == 0:
        return None
    said = probe.stderr.strip().splitlines()
    return said[-1] if said else f"bwrap refused the user-namespace probe ({probe.returncode})"


class UnknownProfile(SpecfloError):
    """An egress class with no sandbox profile; the message names the class."""


@dataclass(frozen=True)
class Profile:
    """The sandbox one egress class gets, beyond the boundary every member has.

    The filesystem boundary is the same for every member, so what a profile
    decides is the network. A member of the strictest class keeps the empty
    network namespace the sandbox starts with and reaches its model through
    a socket bound into it; a member whose prompts leave this host anyway
    needs the host's network to reach its provider, and shares it.
    """

    name: str
    share_net: bool


# One profile per declared egress class. A class added to the pool without
# one is caught by the check that these two agree, not at a member's start.
PROFILES: dict[str, Profile] = {
    "local": Profile("isolated", share_net=False),
    "no-train": Profile("host-network", share_net=True),
    "open": Profile("host-network", share_net=True),
}


def profile_for(egress: str) -> Profile:
    """The profile of egress class *egress*, or a refusal naming it.

    A class that reaches here without a profile is a fault in the pool's own
    code: the classes are declared in one place and the profiles beside them.
    It is refused rather than answered with the strictest profile, which
    would start a member under a boundary nobody chose for it, or with none,
    which would start it under no boundary at all.
    """
    profile = PROFILES.get(egress)
    if profile is None:
        known = ", ".join(sorted(PROFILES))
        raise UnknownProfile(
            f"the egress class '{egress}' has no sandbox profile, so no member of it "
            f"can be started. The classes with one are: {known}."
        )
    return profile


def profile_argv(egress: str) -> list[str]:
    """The flags the profile of *egress* adds to the namespace flags."""
    return ["--share-net"] if profile_for(egress).share_net else []


# Where the host's resolver configuration is named.
RESOLVER = "/etc/resolv.conf"


def resolver_argv(egress: str | None) -> list[str]:
    """The resolver file a member that shares the host's network gets back.

    ``/run`` is covered for every member, since the host's daemons listen
    there on sockets that trust the operator's user: the container engine,
    the system bus, the resolver's own. ``/etc/resolv.conf`` is often a link
    into it, and a member that reaches its provider by name needs that one
    file, read-only. A member with no network needs nothing of it.
    """
    if egress is None or not profile_for(egress).share_net:
        return []
    target = os.path.realpath(RESOLVER)
    if not _within(target, "/run") or not os.path.isfile(target):
        return []
    return ["--ro-bind", target, target]


@dataclass(frozen=True)
class Bridge:
    """How a member with no network reaches its model from inside its sandbox.

    *socket* is the daemon's bridge socket, bound into the sandbox at its own
    path; *port* is the loopback port the member's pi is configured to reach
    its provider on, where the forwarder listens.
    """

    socket: str
    port: int


# How long the forwarder may take to listen, in seconds, and how often the
# launcher looks. It listens within a look or two on this rig.
FORWARDER_WAIT = 5.0
_FORWARDER_LOOK = 0.005

# The launcher a member with a bridge starts as, inside its sandbox. It starts
# the forwarder, waits until the forwarder's socket is listening and becomes
# pi. The wait reads the process network table, which says a socket listens
# without connecting to it: a connection would go through to the bridge and
# reach llama-swap for nothing. The forwarder is a child the launcher leaves
# behind when it becomes pi, and it goes with the sandbox's process namespace
# when pi goes.
_FORWARDER = """port=$1 socket=$2 socat=$3 looks=$4 look=$5
shift 5
"$socat" "TCP-LISTEN:$port,bind=127.0.0.1,fork,reuseaddr" "UNIX-CONNECT:$socket" &
forwarder=$!
listening=": 0100007F:$(printf %04X "$port") 00000000:0000 0A"
until grep -q "$listening" /proc/net/tcp; do
    if ! kill -0 "$forwarder" 2>/dev/null; then
        echo "the forwarder to llama-swap exited before it listened on 127.0.0.1:$port" >&2
        exit 1
    fi
    looks=$((looks - 1))
    if [ "$looks" -le 0 ]; then
        echo "the forwarder to llama-swap did not listen on 127.0.0.1:$port in time" >&2
        exit 1
    fi
    sleep "$look"
done
exec "$@"
"""


def forwarder_argv(bridge: Bridge, socat: str) -> list[str]:
    """The launcher that starts the forwarder to *bridge* and then becomes
    the command after it; *socat* is the forwarder program's path.

    The forwarder listens on 127.0.0.1 alone: the member's namespace has no
    other interface, and a name the member resolves to loopback resolves to
    that address on this rig.
    """
    looks = max(1, int(FORWARDER_WAIT / _FORWARDER_LOOK))
    return [
        "/bin/sh", "-c", _FORWARDER, "forwarder",
        str(bridge.port), bridge.socket, socat, str(looks), str(_FORWARDER_LOOK),
    ]


@dataclass(frozen=True)
class Limits:
    """What one member may use: memory, processor time and processes.

    A value of zero leaves its limit out, so a pool that declares nothing
    gets a sandbox and no ceiling rather than a ceiling of zero.
    """

    memory_mb: int = 0
    cpu_seconds: int = 0
    max_procs: int = 0


# What every member gets where the pool declares nothing of its own.
#
# The process limit is the one worth having: it stops a member that forks
# without end from taking the rig down, and a member needs nothing like 512
# processes to work. The other two are left off on purpose. An address-space
# ceiling counts what the harness reserves rather than what it uses, and the
# harness reserves tens of gigabytes of address space it never touches, so a
# value low enough to catch anything stops it from starting at all. A
# processor-time ceiling ends a member at the moment it has used its seconds,
# and a member is meant to live as long as its lease.
DEFAULT_LIMITS = Limits(max_procs=512)


def prlimit_path() -> str | None:
    """Where prlimit is on this rig, or None when it is not on PATH."""
    return shutil.which("prlimit")


def uid_threads(uid: int | None = None) -> int:
    """The threads this user runs on the whole host, counted from /proc.

    The process limit counts threads rather than processes, and it counts
    them for the user across the host: a user namespace does not give a
    member a count of its own. So a desktop already running a browser is
    most of the number, and a limit below it would stop the sandbox being
    made at all.
    """
    uid = os.getuid() if uid is None else uid
    count = 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        process = os.path.join("/proc", name)
        try:
            if os.stat(process).st_uid == uid:
                count += len(os.listdir(os.path.join(process, "task")))
        except OSError:
            continue
    return count


def limit_argv(limits: Limits, path: str | None = None) -> list[str]:
    """The prlimit prefix applying *limits*, or nothing when none are set.

    Each limit is set as both the soft and the hard one, and a value above
    the hard limit this process inherited is brought down to it: raising a
    hard limit needs a capability the daemon does not have, and asking would
    fail the start rather than the limit.

    The process limit is the user's current thread count plus what the
    member may add plus the sandbox's own processes, since the kernel counts
    threads per user across the host whatever namespace they run in.
    """
    values: dict[int, int] = {}
    if limits.memory_mb:
        values[resource.RLIMIT_AS] = limits.memory_mb * 1024 * 1024
    if limits.cpu_seconds:
        values[resource.RLIMIT_CPU] = limits.cpu_seconds
    if limits.max_procs:
        values[resource.RLIMIT_NPROC] = (
            uid_threads() + limits.max_procs + _SANDBOX_PROCESSES
        )
    if not values:
        return []
    argv = [path or prlimit_path() or "prlimit"]
    for which, value in values.items():
        _, hard = resource.getrlimit(which)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        argv.append(f"{_LIMIT_FLAGS[which]}={value}")
    return argv


def operator_paths(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The operator's paths the sandbox hides, in the order they are mounted.

    Everything is read from *environ*, the daemon's own environment, and
    resolved through its real path: a home-rooted directory that is a symlink
    is hidden where it really is, since a mount on the link would follow it
    and hide the target under a name the member can still reach by another.

    The home comes first and the directories under it after, so each of them
    is a mount of its own on top of the home's. It would be enough to hide
    the home alone, and it would leave a member reading a path that is simply
    not there; a member reading a directory that exists and is empty is the
    same boundary and a far plainer failure.

    The runtime directory comes last. A read-only root still lets a member
    connect to a unix socket, so the daemon's own sockets and the operator's
    session bus are reachable through it until it is covered.
    """
    home = Path(environ.get("HOME") or Path.home())
    named = environ.get(_AGENT_DIR_VAR)
    runtime = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    paths = [
        home,
        *(home / name for name in _UNDER_HOME),
        *([Path(named)] if named else []),
        Path(runtime),
    ]
    return tuple(dict.fromkeys(str(path.resolve()) for path in paths))


def harness_paths(program: str, environ: Mapping[str, str]) -> tuple[str, ...]:
    """The installations the member's harness needs bound back to run at all.

    Sweeping the operator's home takes the harness with it: pi is installed
    under the home on this rig, through a node version manager, and a member
    whose home is a fresh tmpfs has nothing left to start. So the
    installation the member's command names is bound back read-only.

    *program* is the first word of that command, looked up on the member's
    own PATH and then followed link by link to what it really is: a launcher
    on PATH is usually a link into the package that holds the code, and it
    may go through an environment of its own on the way. Every step is taken
    back to its installation - the directory above a ``bin`` that holds it,
    since a harness reads its own library beside its executable, and
    otherwise the directory it sits in - because a step left out is a link
    that leads nowhere once the sandbox is made.

    An installation already on the read-only root and covered by nothing is
    bound again all the same. It costs one mount and it says plainly what a
    member needs, and the sweeps are not the only thing that can take an
    installation away: the fresh temp directory takes one installed under it
    too. The root itself is never bound again.
    """
    found = shutil.which(program, path=environ.get("PATH"))
    if found is None:
        return ()
    paths: list[str] = []
    for candidate in _link_chain(os.path.abspath(found)):
        directory = os.path.dirname(candidate)
        prefix = (
            os.path.dirname(directory)
            if os.path.basename(directory) == "bin"
            else directory
        )
        if prefix == os.sep or not os.path.isdir(prefix):
            continue
        if any(_within(prefix, kept) for kept in paths):
            continue
        paths = [kept for kept in paths if not _within(kept, prefix)]
        paths.append(prefix)
    return tuple(paths)


def _link_chain(path: str, limit: int = 40) -> list[str]:
    """*path* and every link it leads through, ending at what is really there."""
    chain = [path]
    for _ in range(limit):
        try:
            target = os.readlink(chain[-1])
        except OSError:
            break
        step = os.path.abspath(os.path.join(os.path.dirname(chain[-1]), target))
        if step in chain:
            break
        chain.append(step)
    return chain


def empty_file(state_dir: Path | str) -> Path:
    """The empty file the sandbox binds over a hidden file, made if need be.

    It lives beside the state the daemon keeps rather than in a temporary
    directory of its own: a sandbox gives the member a fresh ``/tmp``, and a
    mount source under the host's would be gone from inside the moment the
    member looked.
    """
    made = Path(state_dir) / _EMPTY_NAME
    made.parent.mkdir(parents=True, exist_ok=True)
    if not made.is_file():
        made.write_bytes(b"")
    return made


def hidden_argv(paths: Iterable[str], empty: Path | str) -> list[str]:
    """Mount directives hiding *paths*: a tmpfs on a directory, *empty* on a file.

    A path that is not on the host gets nothing, so bwrap never turns a start
    down over a mount target that was never there. A path that is a symlink
    gets nothing either: the set holds real paths, so a link at one of them
    was planted after the set was worked out, and a mount there would follow
    it to somewhere the sandbox never meant to cover.
    """
    argv: list[str] = []
    for path in paths:
        if os.path.islink(path):
            continue
        if os.path.isdir(path):
            argv += ["--tmpfs", path]
        elif os.path.exists(path):
            argv += ["--ro-bind", str(empty), path]
    return argv


def pin_argv(writable: Iterable[str], hidden: Iterable[str]) -> list[str]:
    """Bind over itself each ancestor of a hidden path lying inside a writable bind.

    A hidden path is a mount point and cannot be renamed, but its parent can
    where the member has a writable bind above it: a working directory
    holding the lease tokens of the project it is in is the case this is
    written for. A member that renames the parent leaves the next sandbox
    nothing to hide at the old path, while what was hidden reads fine under
    the new name. Binding an ancestor over itself makes it a mount point too,
    so a rename or a remove of it fails while it stays as writable as the
    bind around it: a pin gives away nothing the bind had not given already.

    An ancestor is passed over where it is not a directory on the host, or is
    a symlink there - a mount cannot pin a link, it would bind the target
    instead - and where a pin was emitted for it already. A bind is at its
    own path, so a pin is the ancestor over itself and nothing is reached
    through a bind's source that the destination did not already name.
    """
    roots = [os.path.abspath(root) for root in writable]
    paths = [os.path.abspath(path) for path in hidden]
    pinned: set[str] = set(roots)
    argv: list[str] = []
    for root in roots:
        for path in paths:
            for ancestor in _ancestors_within(path, root):
                if ancestor in pinned:
                    continue
                pinned.add(ancestor)
                if not os.path.isdir(ancestor) or os.path.islink(ancestor):
                    continue
                argv += ["--bind", ancestor, ancestor]
    return argv


def _ancestors_within(path: str, root: str) -> list[str]:
    """The ancestors of *path* strictly inside *root*, the outermost first."""
    found: list[str] = []
    parent = os.path.dirname(path)
    while parent != root and _within(parent, root):
        found.append(parent)
        parent = os.path.dirname(parent)
    found.reverse()
    return found


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def base_argv(
    egress: str | None = None,
    hidden: Iterable[str] = (),
    empty: Path | str | None = None,
    writable: Iterable[str] = (),
    readonly: Iterable[str] = (),
    limits: Limits | None = None,
    chdir: Path | str | None = None,
) -> list[str]:
    """The prefix every member's command runs behind, ending with the separator.

    The namespaces are unshared as a set rather than one at a time, so a
    namespace a later bwrap learns to unshare is unshared here too without
    this line changing. The cgroup namespace is the one exception: it is
    tried, because a kernel without it would else turn the whole sandbox
    down. ``--die-with-parent`` is what makes a lease's end final - the
    sandbox goes down with the process that started it, and everything the
    member left running inside goes with it - and ``--new-session`` keeps the
    member off the terminal that started it, which is a herdr pane the
    operator is watching.

    ``/tmp`` and ``/run`` are fresh: the host's daemons listen under ``/run``
    on sockets that trust the operator's user, and a member of a class that
    shares the network gets the resolver file back (``resolver_argv``).

    *hidden* is the set from ``operator_paths``, mounted after the root so it
    overlays it, and *empty* the file a hidden file is covered with. With no
    *empty* a hidden file is passed over and a hidden directory is not: a
    caller that hides nothing needs no file, and one that hides a file and
    gives no file to hide it with is asking for a boundary that is not there.

    *readonly* and *writable* are the paths a member gets back from under
    the swept home, each bound over itself: the harness's own installation
    is the first of them, and the member's working directory and generated
    directory are the ones it may write. A hidden path standing above a bind
    is mounted first and the bind put back on top - that is how a member
    works in a directory under the operator's home while the home itself is
    swept - and every other hidden path comes after both kinds of bind, so
    nothing a bind covers can be brought back. The pins that stop a rename
    inside a writable bind come between the two.

    *chdir* is where the member starts. Without it the sandbox keeps the
    directory it was started from, which is a path that may no longer be
    there once the mounts are made.

    *egress* is the member's egress class, and its profile answers the
    namespace flags: with none named nothing is added, which is the sandbox
    at its strictest. A class with no profile raises rather than returning an
    argv, so a member of one is never started.

    *limits* go in front of the whole thing, as a program of their own.
    prlimit stops reading options at the first argument that is not one, so
    the sandbox follows it with no separator between them.
    """
    hidden = [os.path.abspath(path) for path in hidden]
    writable = [os.path.abspath(path) for path in writable]
    readonly = [os.path.abspath(path) for path in readonly]
    if hidden and empty is None:
        empty = ""
    bound = [*readonly, *writable]
    over = [path for path in hidden if any(_within(root, path) for root in bound)]
    under = [path for path in hidden if path not in over]
    return [
        *(limit_argv(limits) if limits is not None else []),
        bwrap_path() or "bwrap",
        "--unshare-all",
        "--unshare-cgroup-try",
        *(profile_argv(egress) if egress is not None else []),
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        *resolver_argv(egress),
        *(hidden_argv(over, empty) if empty is not None else []),
        *[word for path in readonly for word in ("--ro-bind", path, path)],
        *[word for path in writable for word in ("--bind", path, path)],
        *pin_argv(writable, under),
        *(hidden_argv(under, empty) if empty is not None else []),
        *(["--chdir", str(chdir)] if chdir is not None else []),
        "--",
    ]
