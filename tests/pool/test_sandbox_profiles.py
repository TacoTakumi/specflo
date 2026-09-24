"""The profiles: one per egress class, and a class with none is refused.

An egress class says where what a member is working on may go, and the
profile is how the sandbox makes that true. There is one profile for each
class the pool declares, and a class that reached here without one is a
mistake in the code rather than in a member's definition: it is refused by
name, because the alternative - a sandbox built as if the class were the
strictest, or built without one at all - would be a boundary nobody asked
for either way.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest

from specflo.pool.definitions import EGRESS_CLASSES
from specflo.pool.sandbox import (
    PROFILES,
    UnknownProfile,
    base_argv,
    profile_argv,
    profile_for,
    unavailable,
)


def test_every_declared_egress_class_has_a_profile() -> None:
    assert sorted(PROFILES) == sorted(EGRESS_CLASSES)


def test_each_class_maps_to_exactly_one_profile() -> None:
    for egress in EGRESS_CLASSES:
        assert profile_for(egress) is PROFILES[egress]


def test_a_class_with_no_profile_is_refused_by_name() -> None:
    with pytest.raises(UnknownProfile) as refused:
        profile_for("shared-with-everyone")
    assert "shared-with-everyone" in str(refused.value)


def test_a_class_with_no_profile_never_yields_an_argv() -> None:
    with pytest.raises(UnknownProfile):
        base_argv(egress="shared-with-everyone")


def test_the_local_class_keeps_the_network_the_namespace_flags_took_away() -> None:
    assert profile_argv("local") == []
    assert not profile_for("local").share_net


def test_a_hosted_class_gets_the_host_network_back() -> None:
    for egress in ("no-train", "open"):
        assert profile_argv(egress) == ["--share-net"]
        assert profile_for(egress).share_net


def test_the_profile_flags_come_after_the_namespaces_they_answer() -> None:
    argv = base_argv(egress="open")
    assert argv.index("--unshare-all") < argv.index("--share-net")


def test_a_sandbox_asked_for_no_class_shares_no_network() -> None:
    assert "--share-net" not in base_argv()


def interfaces(argv: list[str]) -> set[str]:
    """The interfaces a process behind *argv* reads in its own network table."""
    read = subprocess.run(
        [*argv, "/bin/sh", "-c", "cat /proc/self/net/dev"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert read.returncode == 0, read.stderr
    return {
        line.split(":")[0].strip()
        for line in read.stdout.splitlines()
        if ":" in line and not line.strip().startswith("face")
    }


def test_a_local_member_sees_loopback_alone_and_a_hosted_one_sees_the_host_s() -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    assert interfaces(base_argv(egress="local")) == {"lo"}
    assert interfaces(base_argv(egress="open")) > {"lo"}


def inside(argv: list[str], script: str) -> str:
    read = subprocess.run(
        [*argv, sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert read.returncode == 0, read.stderr
    return read.stdout


# Lists /run, tries every socket the host has there, and reads the resolver
# file; one line of JSON.
RUN_PROBE = '''import json, os, socket
found = {"run": sorted(os.listdir("/run")), "connected": []}
for path in json.loads(os.environ["HOST_SOCKETS"]):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
        found["connected"].append(path)
    except OSError:
        pass
    finally:
        s.close()
try:
    with open("/etc/resolv.conf", encoding="utf-8") as f:
        found["resolver"] = f.read()
except OSError as exc:
    found["resolver"] = None
print(json.dumps(found))
'''


def host_sockets() -> list[str]:
    """The unix sockets under the host's /run that this user can reach, outside the runtime directory."""
    found = []
    for directory, dirs, files in os.walk("/run", onerror=lambda exc: None):
        if directory.startswith("/run/user"):
            dirs.clear()
            continue
        for name in files:
            path = os.path.join(directory, name)
            try:
                if stat.S_ISSOCK(os.lstat(path).st_mode):
                    found.append(path)
            except OSError:
                pass
    return found


@pytest.mark.parametrize("egress", sorted(PROFILES))
def test_a_member_reaches_no_socket_under_run_and_a_hosted_one_still_resolves(egress) -> None:
    reason = unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    sockets = host_sockets()
    argv = base_argv(egress=egress)

    found = json.loads(inside(
        [argv[0], *argv[1:-1], "--setenv", "HOST_SOCKETS", json.dumps(sockets), "--"],
        RUN_PROBE,
    ))

    assert found["connected"] == []
    resolver = os.path.realpath("/etc/resolv.conf")
    if PROFILES[egress].share_net:
        with open("/etc/resolv.conf", encoding="utf-8") as f:
            assert found["resolver"] == f.read()
        kept = {resolver.split("/")[2]} if resolver.startswith("/run/") else set()
        assert set(found["run"]) == kept
    else:
        assert found["run"] == []
        if resolver.startswith("/run/"):
            assert found["resolver"] is None
