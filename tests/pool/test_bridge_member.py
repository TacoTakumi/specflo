"""A local member's network: loopback alone, and its model through the bridge.

A local member runs in a network namespace of its own, whose one interface is
loopback, so nothing it does reaches past this host. Its model is reached
through the daemon's bridge socket, bound into the sandbox, and a forwarder
inside that gives pi the loopback address its models file names. The
forwarder listens before pi runs, so the first request of a lease never finds
the port closed.

What a member sees is read from inside it: a probe runs as the member's
command, writes down what it found, then becomes the member's pi double. The
network is read from the process network table, which a fresh proc mount
shows for the member's own namespace; the host's interface listing would
still read the daemon's.

Nothing here reaches the rig: the bridge's upstream is an app in this
process, and the one address outside the host a probe tries is refused
before a packet leaves.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from starlette.responses import JSONResponse

from specflo.pool import bridge, launch, runner, sandbox
from specflo.pool.service import PoolService

from .conftest import bridge_served
from .test_runner import DEFINITION, OPERATOR_MODELS, POOL_TOKEN, Rig  # noqa: F401
from .test_runner import rig  # noqa: F401
from .test_runner_sandbox import command_in_front

# Reads what the member's own namespace holds, asks the bridge for the models
# listing and for the log stream at once, and then becomes the member's pi
# double. The port is the one the member's models file names.
PROBE = '''import json, os, socket, stat, sys, urllib.error, urllib.request

out, bound, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
found = {}
with open("/proc/net/dev", encoding="utf-8") as f:
    found["interfaces"] = sorted(line.split(":")[0].strip() for line in f.readlines()[2:])
outward = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
outward.settimeout(2)
try:
    outward.connect(("1.1.1.1", 53))
    found["outward"] = "connected"
except OSError as exc:
    found["outward"] = exc.strerror or str(exc)
finally:
    outward.close()
try:
    found["bound"] = stat.S_ISSOCK(os.stat(bound).st_mode)
except OSError as exc:
    found["bound"] = exc.strerror
for path in ("/v1/models", "/logs/stream"):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=5) as answer:
            found[path] = [answer.status, json.loads(answer.read())]
    except urllib.error.HTTPError as exc:
        found[path] = [exc.code, None]
    except OSError as exc:
        found[path] = [None, str(exc)]
with open(out, "w", encoding="utf-8") as f:
    json.dump(found, f)
os.execv(rest[0], rest)
'''

# Only whether the bridge socket is there, then the member's pi double.
SOCKET_PROBE = '''import json, os, stat, sys

out, bound, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
try:
    there = stat.S_ISSOCK(os.stat(bound).st_mode)
except OSError:
    there = False
with open(out, "w", encoding="utf-8") as f:
    json.dump({"bound": there}, f)
os.execv(rest[0], rest)
'''


def skip_without_a_forwarder() -> None:
    reason = sandbox.unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")
    if shutil.which("socat") is None:
        pytest.skip("socat is not on PATH, so no forwarder can run inside a sandbox")


def upstream() -> httpx.AsyncClient:
    """A llama-swap that answers every request with the path it was asked for."""
    async def answer(scope, receive, send) -> None:
        if scope["type"] == "http":
            await JSONResponse({"served": scope["path"]})(scope, receive, send)

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=answer), base_url="http://llama-swap"
    )


def served(path: Path):
    """The bridge served at *path*, its upstream the stub above."""
    return bridge_served(path, upstream())


def written(path: Path, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.02)
    raise AssertionError(f"the probe wrote nothing to {path}")


@pytest.fixture
def socket_path(tmp_path_factory) -> Path:
    # Outside the rig's directory, which the sandbox binds back whole as the
    # installation of the rig's harness: a socket there is in every member.
    return tmp_path_factory.mktemp("daemon") / bridge.SOCKET_NAME


def probed(rig, socket_path: Path, out: Path):
    return replace(
        rig.local_member(),
        command=command_in_front(rig, PROBE, "probe.py", f"{out} {socket_path}"),
    )


def test_a_local_member_sees_loopback_alone_and_cannot_reach_out(rig, socket_path) -> None:
    skip_without_a_forwarder()
    out = rig.work / "probe.json"

    with served(socket_path):
        rig.start(probed(rig, socket_path, out), bridge=socket_path)
        found = written(out)

    assert found["interfaces"] == ["lo"]
    assert found["outward"] != "connected"


def test_the_bridge_socket_is_bound_into_the_sandbox_and_reached_through_it(
    rig, socket_path
) -> None:
    skip_without_a_forwarder()
    out = rig.work / "probe.json"

    with served(socket_path):
        rig.start(probed(rig, socket_path, out), bridge=socket_path)
        found = written(out)

    assert found["bound"] is True
    assert found["/v1/models"] == [200, {"served": "/v1/models"}]
    # Refused on the host side, so the request went through the filter.
    assert found["/logs/stream"] == [bridge.REFUSED, None]


def test_the_first_request_of_every_lease_succeeds_over_repeated_starts(
    rig, socket_path
) -> None:
    skip_without_a_forwarder()

    with served(socket_path):
        for start in range(10):
            out = rig.work / f"probe-{start}.json"
            name = rig.start(probed(rig, socket_path, out), bridge=socket_path)
            found = written(out)
            runner.stop(name, "released", pool_token=POOL_TOKEN)
            assert found["/v1/models"] == [200, {"served": "/v1/models"}], f"start {start}"


def test_a_forwarder_slow_to_listen_is_listening_before_pi_runs(rig, socket_path) -> None:
    skip_without_a_forwarder()
    # The forwarder, found on the daemon's PATH ahead of the real one, takes
    # far longer to listen than the member takes to ask: a launcher that did
    # not wait for it would hand pi a closed port.
    slow = rig.harness / "bin" / "socat"
    slow.write_text(f"#!/bin/sh\nsleep 0.3\nexec {shutil.which('socat')} \"$@\"\n")
    slow.chmod(0o755)
    out = rig.work / "probe.json"

    with served(socket_path):
        rig.start(probed(rig, socket_path, out), bridge=socket_path)
        found = written(out)

    assert found["/v1/models"] == [200, {"served": "/v1/models"}]


def test_a_forwarder_that_never_listens_fails_the_start_saying_so(
    rig, socket_path, monkeypatch
) -> None:
    skip_without_a_forwarder()
    # The forwarder gives up after the member's host is up, and a member in a
    # scope of its own gets there a little later, so the start watches pi for
    # as long as it does when shipped.
    rig.watch_as_shipped(monkeypatch)
    broken = rig.harness / "bin" / "socat"
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)

    with served(socket_path):
        with pytest.raises(runner.RunnerError, match="forwarder"):
            rig.start(rig.local_member(), bridge=socket_path)


def test_a_local_member_is_refused_when_the_bridge_is_not_served(rig, socket_path) -> None:
    with pytest.raises(launch.LaunchError, match="bridge"):
        rig.start(rig.local_member(), bridge=socket_path)
    assert rig.launch_leftovers("local-1") == []


def test_a_local_member_whose_provider_is_not_on_loopback_is_refused(
    rig, socket_path
) -> None:
    models = json.loads(json.dumps(OPERATOR_MODELS))
    for provider in models["providers"].values():
        provider["baseUrl"] = "http://rig.example:8080/v1"
    rig.models_file.write_text(json.dumps(models), encoding="utf-8")

    with served(socket_path):
        with pytest.raises(launch.LaunchError, match="rig.example"):
            rig.start(rig.local_member(), bridge=socket_path)
    assert rig.launch_leftovers("local-1") == []


def test_a_hosted_member_is_given_no_bridge(rig, socket_path) -> None:
    reach = sandbox.Bridge(socket=str(socket_path), port=8080)
    with pytest.raises(launch.LaunchError, match="hosted-1"):
        launch.member_argv(
            DEFINITION, rig.hosted_member(),
            {"PATH": "/usr/bin:/bin", "HOME": str(rig.tmp_path)},
            cwd=rig.work, state_dir=rig.tmp_path, bridge=reach,
        )


def test_a_hosted_member_started_beside_a_bridge_does_not_get_it(rig, socket_path) -> None:
    skip_without_a_forwarder()
    out = rig.work / "probe.json"
    # A hosted member shares the host's network, so this probe asks nothing
    # of it: a request to loopback would reach whatever the host runs there.
    member = replace(
        rig.hosted_member(),
        command=command_in_front(rig, SOCKET_PROBE, "probe.py", f"{out} {socket_path}"),
    )

    with served(socket_path):
        rig.start(member, bridge=socket_path)
        found = written(out)

    assert found == {"bound": False}


def test_the_forwarder_stands_between_the_sandbox_and_pi(rig, socket_path) -> None:
    reach = sandbox.Bridge(socket=str(socket_path), port=8080)
    member = rig.local_member()
    argv = launch.member_argv(
        DEFINITION, member, {"PATH": "/usr/bin:/bin", "HOME": str(rig.tmp_path)},
        cwd=rig.work, state_dir=rig.tmp_path, bridge=reach,
    )
    separator = argv.index("--")
    assert ["--ro-bind", str(socket_path), str(socket_path)] == [
        argv[i] for i in range(argv.index(str(socket_path)) - 1, argv.index(str(socket_path)) + 2)
    ]
    after = argv[separator + 1 :]
    assert after[-len(launch.pi_argv(DEFINITION, member)) :] == launch.pi_argv(DEFINITION, member)
    assert "8080" in after and str(socket_path) in after


def test_the_service_hands_the_bridge_to_every_start(tmp_path, monkeypatch) -> None:
    from specflo.daemon import pool_routes
    from specflo.pool.cli_admin import pool_dir

    root = tmp_path / "root"
    root.mkdir()
    pool_dir(root).mkdir()
    monkeypatch.setattr(pool_routes, "load_pool_config", lambda directory: (object(), []))
    monkeypatch.setattr(pool_routes, "attached_names", lambda config, store: [])
    monkeypatch.setattr(pool_routes.waiting, "forget_all", lambda service: None)

    service, faults = pool_routes.open_pool(root)

    assert faults == ()
    assert isinstance(service, PoolService)
    assert service.bridge == bridge.socket_path(root)


def test_a_grant_starts_its_member_with_the_service_s_bridge(pool_rig, monkeypatch) -> None:
    from dataclasses import replace as replaced

    started = []

    def start(definition, member, accounts, **kwargs):
        started.append(kwargs.get("bridge"))
        return member.name

    monkeypatch.setattr(runner, "start", start)
    config = pool_rig.config(pool_rig.local_member())
    service = replaced(pool_rig.service(config), bridge=pool_rig.tmp_path / "llama-swap.sock")

    service.grant("rebasers", holder_label="a holder", cwd=pool_rig.work)

    assert started == [pool_rig.tmp_path / "llama-swap.sock"]
