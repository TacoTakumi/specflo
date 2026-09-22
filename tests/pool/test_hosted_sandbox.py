"""A hosted member: the filesystem boundary of every member, on the host's network.

A hosted member's prompts go to its provider over the network, so it keeps
the host's network namespace. What it does not keep is the operator's
filesystem: the hidden set is the same for it as for a local member, and it
is read here from inside a member started by the one builder every start
goes through, with the command line a hosted member gets.

The operator's corners are laid out under a temporary home, so nothing here
reads the operator's own. The network check connects to a listener this test
opens on the host's loopback: a member in a namespace of its own would find
nothing there.

Whether the provider itself answers a connection from inside is checked only
when the operator asks for it, by setting the variable ``PROVIDER_CHECK``
names: it opens a connection off this host.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from specflo.pool import launch, sandbox
from specflo.pool.config import Member

from .test_runner import DEFINITION

# Set to 1 to have a hosted member connect to its provider from inside.
PROVIDER_CHECK = "SPECFLO_POOL_CHECK_PROVIDER"
PROVIDER = ("openrouter.ai", 443)

# Lists each directory it is given, tries to read each file, connects to each
# address, and writes down what it found. The member's own command line
# follows its arguments, and it ignores that.
PROBE = '''import json, os, socket, sys

out = sys.argv[1]
asked = json.loads(sys.argv[2])
found = {"listed": {}, "read": {}, "connected": {}}
for path in asked["directories"]:
    try:
        found["listed"][path] = sorted(os.listdir(path))
    except OSError as exc:
        found["listed"][path] = exc.strerror
for path in asked["files"]:
    try:
        with open(path, encoding="utf-8") as f:
            found["read"][path] = f.read()
    except OSError as exc:
        found["read"][path] = exc.strerror
for host, port in asked["addresses"]:
    reach = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reach.settimeout(10)
    try:
        reach.connect((host, port))
        found["connected"][f"{host}:{port}"] = True
    except OSError as exc:
        found["connected"][f"{host}:{port}"] = exc.strerror or str(exc)
    finally:
        reach.close()
found["net"] = os.readlink("/proc/self/ns/net")
with open(out, "w", encoding="utf-8") as f:
    json.dump(found, f)
'''


def skip_without_a_sandbox() -> None:
    reason = sandbox.unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home laid out like the operator's, with something in each corner."""
    home = tmp_path / "home"
    for relative in (".pi/agent", ".agents/skills/a-skill", ".specflo/agents/one"):
        (home / relative).mkdir(parents=True)
    (home / ".pi/agent/auth.json").write_text('{"key": "secret"}', encoding="utf-8")
    (home / ".agents/skills/a-skill/SKILL.md").write_text("marker", encoding="utf-8")
    (home / ".specflo/agents/one/sock").write_text("", encoding="utf-8")
    (home / "notes.txt").write_text("the operator's", encoding="utf-8")
    return home


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "bus").write_text("", encoding="utf-8")
    return runtime


@pytest.fixture
def listener():
    """A listener on the host's loopback, for a member to find or not."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        yield server.getsockname()


def hosted(tmp_path: Path, asked: dict) -> tuple[Member, Path]:
    """A hosted member whose command is the probe, and where it writes."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    script = work / "probe.py"
    script.write_text(PROBE, encoding="utf-8")
    out = work / "found.json"
    member = Member(
        name="hosted-1", command="python", backing="hosted", labels=(), capacity=1,
        egress="no-train", model="some-vendor/some-model", account="team-a",
    )
    command = " ".join([sys.executable, str(script), str(out), _quoted(json.dumps(asked))])
    return replace(member, command=command), out


def _quoted(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def run(member: Member, tmp_path: Path, environ: dict[str, str], out: Path) -> dict:
    """Start *member* behind the argv the launch builder makes, and read the probe."""
    argv = launch.member_argv(
        DEFINITION, member, environ, cwd=tmp_path / "work", state_dir=tmp_path / "state",
    )
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_a_hosted_member_finds_the_hidden_set_empty_and_reaches_the_host_network(
    tmp_path, home, runtime, listener
) -> None:
    skip_without_a_sandbox()
    environ = {"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime), "PATH": os.environ["PATH"]}
    corners = [str(home / name) for name in (".pi", ".agents", ".specflo")]
    member, out = hosted(tmp_path, {
        "directories": [str(home), *corners, str(runtime)],
        "files": [str(home / ".pi/agent/auth.json"), str(home / "notes.txt")],
        "addresses": [list(listener)],
    })

    found = run(member, tmp_path, environ, out)

    # every hidden directory is there and empty; the home holds only their
    # mount points
    for path in (*corners, str(runtime)):
        assert found["listed"][path] == [], path
    assert found["listed"][str(home)] == [".agents", ".pi", ".specflo"]
    for path in found["read"]:
        assert found["read"][path] != "the operator's"
        assert "secret" not in found["read"][path]
    # and the host's network is the member's own
    assert found["connected"] == {f"{listener[0]}:{listener[1]}": True}
    assert found["net"] == os.readlink("/proc/self/ns/net")


def test_a_hosted_member_reaches_its_provider(tmp_path, home, runtime) -> None:
    if os.environ.get(PROVIDER_CHECK) != "1":
        pytest.skip(
            f"connects off this host to {PROVIDER[0]}; set {PROVIDER_CHECK}=1 to run it"
        )
    skip_without_a_sandbox()
    environ = {"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime), "PATH": os.environ["PATH"]}
    member, out = hosted(tmp_path, {
        "directories": [], "files": [], "addresses": [list(PROVIDER)],
    })

    found = run(member, tmp_path, environ, out)

    assert found["connected"] == {f"{PROVIDER[0]}:{PROVIDER[1]}": True}
