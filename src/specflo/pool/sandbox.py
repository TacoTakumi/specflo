"""The bubblewrap sandbox a member runs inside: the argv the daemon starts it behind.

A member's egress class is a claim about where what it is working on can go,
and until now it was a claim about a list: the environment the pool handed
over, the flags on the command line. What the member actually loaded was
decided by the machine, so the claim held only as far as the list did. The
sandbox makes the class a property of the namespaces the process runs in,
which no extension, skill or subprocess of the member can argue with.

This module builds the argv and nothing else. It starts no process, reads no
configuration and asks the caller for nothing it could take from the
surroundings, so the same rig gives the same argv every time and a test can
read the whole boundary without making one. The caller appends the member's
own command after the separator the prefix ends with.

Later mounts overlay earlier ones, so the order is the boundary:

1. the namespace and process-safety flags
2. the read-only root, then a fresh dev, proc and temp over it
3. the hidden set: a tmpfs over each of the operator's directories and an
   empty file over each of its files

A fresh ``/tmp`` matters more than it looks: it is the one directory every
tool writes to, so a member sharing the host's would read what the operator's
own work left there and leave its own behind for the next lease.

The sandbox is only as good as the kernel and the policy under it. On a rig
where an unprivileged user namespace cannot be made, bwrap refuses and there
is no boundary to have; ``unavailable`` says so in one line, for a test to
skip on and for a start to refuse with.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

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
        probe = subprocess.run(
            [path, *_PROBE],
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


def base_argv(
    hidden: Iterable[str] = (),
    empty: Path | str | None = None,
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

    *hidden* is the set from ``operator_paths``, mounted after the root so it
    overlays it, and *empty* the file a hidden file is covered with. With no
    *empty* a hidden file is passed over and a hidden directory is not: a
    caller that hides nothing needs no file, and one that hides a file and
    gives no file to hide it with is asking for a boundary that is not there.
    """
    hidden = list(hidden)
    if hidden and empty is None:
        empty = ""
    return [
        bwrap_path() or "bwrap",
        "--unshare-all",
        "--unshare-cgroup-try",
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
        *(hidden_argv(hidden, empty) if empty is not None else []),
        "--",
    ]
