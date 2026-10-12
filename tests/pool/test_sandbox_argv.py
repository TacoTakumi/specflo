"""The sandbox prefix a member's command runs behind: namespaces and the root.

The checks are on the argv the builder returns, because that is what the
daemon hands to a process start, and one run of ``true`` behind the prefix to
show the argv is a sandbox bwrap accepts rather than a list that reads well.
The run skips where the rig cannot make a sandbox at all, and says why: the
unprivileged user namespace is a kernel and policy setting, not this code.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from specflo.pool.sandbox import (
    DNS_FORWARDER,
    RESOLVER,
    WEB_PROXY,
    Bridge,
    base_argv,
    dns_argv,
    forwarder_argv,
    resolver_file,
    resolver_target,
    unavailable,
    web_port,
)
from waits import wait_until


def flag_index(argv: list[str], flag: str) -> int:
    """Where *flag* stands in *argv*; fails the test when it does not."""
    assert flag in argv, f"{flag} is not in the sandbox argv"
    return argv.index(flag)


def pairs(argv: list[str], flag: str) -> list[tuple[str, ...]]:
    """Every use of *flag* in *argv* with the arguments that follow it."""
    width = {"--ro-bind": 2, "--bind": 2, "--tmpfs": 1, "--dev": 1, "--proc": 1}[flag]
    return [
        tuple(argv[index + 1 : index + 1 + width])
        for index, word in enumerate(argv)
        if word == flag
    ]


def test_it_starts_with_bwrap() -> None:
    assert base_argv()[0].endswith("bwrap")


def test_it_unshares_every_namespace() -> None:
    argv = base_argv()
    assert "--unshare-all" in argv
    assert "--unshare-cgroup-try" in argv


def test_it_dies_with_its_parent_and_starts_a_new_session() -> None:
    argv = base_argv()
    assert "--die-with-parent" in argv
    assert "--new-session" in argv


def test_it_binds_the_root_read_only() -> None:
    assert ("/", "/") in pairs(base_argv(), "--ro-bind")
    assert pairs(base_argv(), "--bind") == []


def test_it_gives_the_sandbox_a_fresh_dev_proc_and_temp() -> None:
    argv = base_argv()
    assert pairs(argv, "--dev") == [("/dev",)]
    assert pairs(argv, "--proc") == [("/proc",)]
    assert ("/tmp",) in pairs(argv, "--tmpfs")


def test_the_fresh_mounts_come_after_the_root_so_they_overlay_it() -> None:
    argv = base_argv()
    root = flag_index(argv, "--ro-bind")
    for flag in ("--dev", "--proc", "--tmpfs"):
        assert flag_index(argv, flag) > root


def test_it_ends_with_the_separator_so_a_command_can_follow() -> None:
    assert base_argv()[-1] == "--"


def test_the_builder_reads_nothing_from_the_caller_s_environment(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/nowhere")
    monkeypatch.setenv("TMPDIR", "/nowhere")
    first = base_argv(egress="open")
    monkeypatch.setenv("HOME", "/elsewhere")
    monkeypatch.setenv("TMPDIR", "/elsewhere")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/elsewhere")
    assert base_argv(egress="open") == first


def test_a_command_behind_the_prefix_runs_and_is_in_its_own_namespaces() -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    script = "readlink /proc/self/ns/mnt /proc/self/ns/user /proc/self/ns/pid"
    inside = subprocess.run(
        [*base_argv(), "/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert inside.returncode == 0, inside.stderr
    outside = subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, timeout=60
    )
    assert inside.stdout.split() != []
    for sandboxed, host in zip(inside.stdout.split(), outside.stdout.split()):
        assert sandboxed != host


def test_the_sandbox_gets_a_temp_of_its_own() -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    marker = "specflo-sandbox-argv-marker"
    inside = subprocess.run(
        [*base_argv(), "/bin/sh", "-c", f"touch /tmp/{marker} && ls /tmp"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert inside.returncode == 0, inside.stderr
    assert inside.stdout.split() == [marker]


def _listening(table: str, port: int) -> bool:
    """Whether the process network table *table* has a socket listening on
    127.0.0.1:*port*."""
    return f": 0100007F:{port:04X} 00000000:0000 0A" in table


def _processes_naming(text: str) -> list[int]:
    """The live processes whose command line holds *text*; only read, to see
    that none is left."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{name}/cmdline").read_bytes().replace(b"\0", b" ")
            state = Path(f"/proc/{name}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            continue
        if text.encode() in cmdline and state not in ("Z", "X"):
            found.append(int(name))
    return found


def test_two_forwarders_listen_before_the_command_and_go_with_the_sandbox() -> None:
    """A web member's launcher: the forwarder to llama-swap, then the one to
    its lease's web proxy, each on a loopback port of its own in a sandbox
    with no network, both listening before the command runs, and both gone
    once the command ends."""
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    socat = shutil.which("socat")
    if socat is None:
        pytest.skip("socat is not on PATH, so no forwarder can run inside a sandbox")
    # A short directory of its own: a unix socket path holds 107 bytes.
    directory = tempfile.mkdtemp(dir="/tmp", prefix="specflo-fwd-")
    servers = []
    try:
        reaches = []
        for name, port, to in (("bridge.sock", 8080, "llama-swap"),
                               ("web.sock", web_port(8080), WEB_PROXY)):
            path = os.path.join(directory, name)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen()
            servers.append(server)
            reaches.append(Bridge(socket=path, port=port, to=to))
        argv = [
            *base_argv(egress="local", readonly=[reach.socket for reach in reaches]),
            *forwarder_argv(reaches[0], socat),
            *forwarder_argv(reaches[1], socat),
            "/bin/cat", "/proc/net/tcp",
        ]

        inside = subprocess.run(argv, capture_output=True, text=True, timeout=60)

        assert inside.returncode == 0, inside.stderr
        for reach in reaches:
            assert _listening(inside.stdout, reach.port), f"nothing listens on {reach.port}"
        # The forwarders went with the sandbox's process namespace.
        assert wait_until(lambda: _processes_naming(directory) == [], timeout=10.0)
    finally:
        for server in servers:
            server.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_a_forwarder_that_fails_says_what_it_forwards_to() -> None:
    reach = Bridge(socket="/nowhere/web.sock", port=3128, to=WEB_PROXY)

    argv = forwarder_argv(reach, "/bin/false")

    assert WEB_PROXY in argv
    assert "llama-swap" not in argv


# -- DNS inside a web member's sandbox ---------------------------------------
#
# A stub resolver asks the nameserver its resolver file names, on port 53,
# and the file cannot name another port. A web member's sandbox gets a file
# of its own over the host's, naming 127.0.0.1, and a forwarder on 127.0.0.1
# port 53 to its lease's DNS socket. A port under 1024 is bound with a
# capability over the network namespace, which the member never holds: the
# launcher makes a network namespace of its own inside the sandbox's, binds
# the port there as the root of a user namespace that owns it, and runs the
# rest in a user namespace below that one, as the operator's uid again and
# with no capability at all.


def test_the_resolver_file_names_the_loopback_alone(tmp_path) -> None:
    made = resolver_file(tmp_path)

    assert made.parent == tmp_path
    assert made.read_text(encoding="utf-8") == "nameserver 127.0.0.1\n"
    # made once, and left as it is after
    assert resolver_file(tmp_path) == made


def test_the_resolver_file_goes_where_the_host_s_resolver_file_leads() -> None:
    # /etc/resolv.conf is often a link into /run, which the sandbox mounts
    # fresh, so the file is bound where the link leads
    assert resolver_target() == os.path.realpath(RESOLVER)


def test_a_file_given_is_bound_read_only_over_its_path_after_the_fresh_mounts() -> None:
    argv = base_argv(files=[("/state/resolv.conf", "/run/systemd/resolve/stub-resolv.conf")])

    assert ("/state/resolv.conf", "/run/systemd/resolve/stub-resolv.conf") in pairs(
        argv, "--ro-bind"
    )
    bound = argv.index("/state/resolv.conf")
    assert bound > max(i for i, word in enumerate(argv) if word == "--tmpfs")
    assert "/state/resolv.conf" not in base_argv()


def test_the_dns_launcher_says_what_it_forwards_to() -> None:
    argv = dns_argv("/nowhere/web.dns", "/bin/false", "/usr/bin/unshare", "/usr/sbin/ip")

    assert "/nowhere/web.dns" in argv
    assert DNS_FORWARDER in argv


def test_the_dns_forwarder_listens_on_port_53_and_the_command_runs_unprivileged() -> None:
    """The launcher in a sandbox with no network: the forwarder listens on
    127.0.0.1 port 53 before the command runs, the resolver file names it,
    and the command runs as the operator's uid and gid with no capability."""
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    tools = [shutil.which(name, path="/usr/bin:/bin:/usr/sbin:/sbin")
             for name in ("socat", "unshare", "ip")]
    if None in tools:
        pytest.skip("socat, unshare or ip is not on PATH, so no DNS forwarder can run")
    directory = tempfile.mkdtemp(dir="/tmp", prefix="specflo-dns-")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        path = os.path.join(directory, "web.dns")
        server.bind(path)
        server.listen()
        made = str(resolver_file(directory))
        argv = [
            *base_argv(egress="local", readonly=[path], files=[(made, resolver_target())]),
            *dns_argv(path, *tools),
            "/bin/sh", "-c",
            "cat /proc/net/udp; id -u; id -g; grep CapEff /proc/self/status; cat /etc/resolv.conf",
        ]

        inside = subprocess.run(argv, capture_output=True, text=True, timeout=60)

        assert inside.returncode == 0, inside.stderr
        assert ": 0100007F:0035 00000000:0000 07" in inside.stdout
        lines = inside.stdout.splitlines()
        assert str(os.getuid()) in lines and str(os.getgid()) in lines
        assert "CapEff:\t0000000000000000" in lines
        assert "nameserver 127.0.0.1" in lines
        # The forwarder went with the sandbox's process namespace.
        assert wait_until(lambda: _processes_naming(directory) == [], timeout=10.0)
    finally:
        server.close()
        shutil.rmtree(directory, ignore_errors=True)
