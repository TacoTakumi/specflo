"""A local member with web access: its lease's proxy socket, a forwarder, and the variables.

A member leased under a definition with web access has the proxy socket of
its own lease bound into its sandbox, and a second forwarder inside, on a
loopback port of its own, brings it to the member's tools. The proxy
variables point at that port, and the no-proxy variables name the provider's
host, so the member's model traffic still goes through the forwarder to the
bridge. A member without web access gets none of this.

What a member sees is read from inside it: a probe runs as the member's
command, writes down what it found, then becomes the member's pi double. The
proxy here is a stub that answers every request with the request line it was
sent, so nothing leaves this host.
"""

from __future__ import annotations

import contextlib
import json
import os
import socketserver
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from specflo.pool import launch, runner

from .test_bridge_member import served, skip_without_a_forwarder, written
from .test_runner import DEFINITION, POOL_TOKEN, Rig  # noqa: F401
from .test_runner import rig  # noqa: F401
from .test_runner_sandbox import command_in_front
from waits import wait_until

# Writes down the member's proxy variables, whether the web socket is bound,
# what a request through the proxy variables and one to the provider's port
# came back with, then becomes the member's pi double. urllib reads the proxy
# variables as most tools do.
PROBE = '''import json, os, stat, sys, urllib.error, urllib.request

out, bound, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
names = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy")
found = {"env": {name: os.environ[name] for name in names if name in os.environ}}
try:
    found["bound"] = stat.S_ISSOCK(os.stat(bound).st_mode)
except OSError:
    found["bound"] = False
for key, url in (("web", "http://example.invalid/page"),
                 ("model", "http://127.0.0.1:8080/v1/models")):
    try:
        with urllib.request.urlopen(url, timeout=5) as answer:
            found[key] = [answer.status, json.loads(answer.read())]
    except urllib.error.HTTPError as exc:
        found[key] = [exc.code, None]
    except OSError as exc:
        found[key] = [None, str(exc)]
with open(out, "w", encoding="utf-8") as f:
    json.dump(found, f)
os.execv(rest[0], rest)
'''


class _Answer(socketserver.StreamRequestHandler):
    """Answers one request with the request line it was sent."""

    def handle(self) -> None:
        line = self.rfile.readline().decode("latin-1").strip()
        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
            pass
        body = json.dumps({"asked": line}).encode()
        self.wfile.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )


@contextlib.contextmanager
def proxy_served(path: Path):
    """A stub web proxy on the unix socket *path*, gone when the block ends."""
    path.parent.mkdir(mode=0o700, exist_ok=True)
    server = socketserver.ThreadingUnixStreamServer(str(path), _Answer)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)
        path.unlink(missing_ok=True)


def forwarders_to(path: Path) -> list[int]:
    """The live forwarders to the socket *path*, found by the argument socat
    is started with; only read, to see whether one runs."""
    text = f"UNIX-CONNECT:{os.path.realpath(path)}"
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


@pytest.fixture
def daemon(tmp_path_factory) -> Path:
    # Outside the rig's directory, which the sandbox binds back whole as the
    # installation of the rig's harness: a socket there is in every member.
    return tmp_path_factory.mktemp("daemon")


def probed(rig, out: Path, web_socket: Path):
    return replace(
        rig.local_member(),
        command=command_in_front(rig, PROBE, "probe.py", f"{out} {web_socket}"),
    )


def test_a_web_member_reaches_the_web_through_its_lease_s_proxy_and_its_model_directly(
    rig, daemon
) -> None:
    skip_without_a_forwarder()
    out = rig.work / "probe.json"
    web_socket = daemon / "web" / "lease-1.sock"

    with served(daemon / "bridge.sock"), proxy_served(web_socket):
        name = rig.start(probed(rig, out, web_socket), bridge=daemon / "bridge.sock",
                         web=web_socket)
        found = written(out)
        assert forwarders_to(web_socket) != []
        runner.stop(name, "released", pool_token=POOL_TOKEN)

    port = found["env"]["HTTPS_PROXY"].rsplit(":", 1)[1]
    assert found["env"] == {
        **{name: f"http://127.0.0.1:{port}" for name in launch.PROXY_ENV},
        **{name: "127.0.0.1" for name in launch.NO_PROXY_ENV},
    }
    assert port != "8080"
    assert found["bound"] is True
    assert found["web"] == [200, {"asked": "GET http://example.invalid/page HTTP/1.1"}]
    assert found["model"] == [200, {"served": "/v1/models"}]
    # The forwarder to the proxy went with the lease.
    assert wait_until(lambda: forwarders_to(web_socket) == [], timeout=10.0)


def test_a_member_without_web_access_gets_no_socket_no_forwarder_and_no_variables(
    rig, daemon
) -> None:
    skip_without_a_forwarder()
    out = rig.work / "probe.json"
    web_socket = daemon / "web" / "lease-1.sock"

    with served(daemon / "bridge.sock"), proxy_served(web_socket):
        name = rig.start(probed(rig, out, web_socket), bridge=daemon / "bridge.sock")
        try:
            found = written(out)
            assert forwarders_to(web_socket) == []
        finally:
            # Ended by its lease, as a pool ends it: the rig's kill would leave
            # the host's socket behind.
            runner.stop(name, "released", pool_token=POOL_TOKEN)

    assert found["env"] == {}
    assert found["bound"] is False
    assert found["web"][0] is None
    assert found["model"] == [200, {"served": "/v1/models"}]


def test_a_web_member_whose_proxy_is_not_served_is_refused(rig, daemon) -> None:
    web_socket = daemon / "web" / "lease-1.sock"

    with served(daemon / "bridge.sock"):
        with pytest.raises(launch.LaunchError, match="web proxy"):
            rig.start(rig.local_member(), bridge=daemon / "bridge.sock", web=web_socket)
    assert rig.launch_leftovers("local-1") == []


def test_a_web_member_whose_proxy_path_is_no_socket_is_refused(rig, daemon) -> None:
    web_socket = daemon / "web" / "lease-1.sock"
    web_socket.parent.mkdir()
    web_socket.write_text("", encoding="utf-8")

    with served(daemon / "bridge.sock"):
        with pytest.raises(launch.LaunchError, match="web proxy"):
            rig.start(rig.local_member(), bridge=daemon / "bridge.sock", web=web_socket)
