"""The filter holds against a member that goes around its own forwarder.

The forwarder inside a member's sandbox is the member's: it runs as the
member, in the member's namespaces, and the member may kill it. The socket
behind it is bound into the sandbox too, so a member that wants more of
llama-swap than the forwarder passes can stop it and speak to the socket
with a client of its own. What answers there is the daemon's filter, on the
host side, which the member cannot reach into.

The probe here does exactly that from inside a started member: it stops the
forwarder, checks that the loopback port is closed, and then asks the bound
socket directly for what a member may and may not have.

Nothing here reaches the rig: the bridge's upstream is an app in this
process.
"""

from __future__ import annotations

from dataclasses import replace

from .test_bridge_member import (  # noqa: F401  (fixtures)
    rig,
    served,
    skip_without_a_forwarder,
    socket_path,
    written,
)
from .test_runner_sandbox import command_in_front

from specflo.pool import bridge

# Stops every forwarder in the member's process namespace, then speaks HTTP
# over the bound socket itself, and becomes the member's pi double.
PROBE = '''import json, os, signal, socket, sys, time

out, bound, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
found = {"stopped": []}
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
            name = f.read().strip()
    except OSError:
        continue
    if name == "socat":
        os.kill(int(pid), signal.SIGKILL)
        found["stopped"].append(int(pid))
time.sleep(0.2)
port = socket.socket()
try:
    port.connect(("127.0.0.1", 8080))
    found["port"] = "open"
except OSError as exc:
    found["port"] = exc.strerror
finally:
    port.close()

def ask(method, path):
    direct = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    direct.settimeout(5)
    direct.connect(bound)
    direct.sendall(
        f"{method} {path} HTTP/1.1\\r\\nHost: llama-swap\\r\\n"
        "Content-Length: 0\\r\\nConnection: close\\r\\n\\r\\n".encode()
    )
    answer = b""
    while chunk := direct.recv(65536):
        answer += chunk
    direct.close()
    return int(answer.split(b" ", 2)[1])

for method, path in (
    ("GET", "/logs/stream"),
    ("GET", "/running"),
    ("POST", "/api/models/unload"),
    ("GET", "/v1/models/../../logs/stream"),
    ("GET", "/v1/models"),
):
    found[f"{method} {path}"] = ask(method, path)
with open(out, "w", encoding="utf-8") as f:
    json.dump(found, f)
os.execv(rest[0], rest)
'''


def test_a_member_that_stops_its_forwarder_is_refused_at_the_socket(rig, socket_path) -> None:
    skip_without_a_forwarder()
    out = rig.work / "probe.json"
    member = replace(
        rig.local_member(),
        command=command_in_front(rig, PROBE, "probe.py", f"{out} {socket_path}"),
    )

    with served(socket_path):
        rig.start(member, bridge=socket_path)
        found = written(out)

    # the forwarder was there, and is gone
    assert found["stopped"] != []
    assert found["port"] != "open"
    # what the filter refuses through the forwarder it refuses here too
    assert found["GET /logs/stream"] == bridge.REFUSED
    assert found["GET /running"] == bridge.REFUSED
    assert found["POST /api/models/unload"] == bridge.REFUSED
    assert found["GET /v1/models/../../logs/stream"] == bridge.REFUSED
    # and the socket is the filter, not a closed door: an allowed request passes
    assert found["GET /v1/models"] == 200
