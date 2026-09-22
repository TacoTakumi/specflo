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

A fresh ``/tmp`` matters more than it looks: it is the one directory every
tool writes to, so a member sharing the host's would read what the operator's
own work left there and leave its own behind for the next lease.

The sandbox is only as good as the kernel and the policy under it. On a rig
where an unprivileged user namespace cannot be made, bwrap refuses and there
is no boundary to have; ``unavailable`` says so in one line, for a test to
skip on and for a start to refuse with.
"""

from __future__ import annotations

import shutil
import subprocess

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


def base_argv() -> list[str]:
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
    """
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
        "--",
    ]
