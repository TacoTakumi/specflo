"""Every member starts inside the sandbox, and nothing of it outlives the lease.

Three things are checked here. A started member is in namespaces of its own,
read from inside the member rather than from the daemon. There is one way to
build a member's start, so a second one cannot appear that keeps the flags and
loses the boundary. And a process a member leaves running in the background is
gone when the lease ends, which is what the sandbox dying with its parent is
for.
"""

from __future__ import annotations

import ast
import json
import os
import shlex
import time
from pathlib import Path

import pytest

from specflo.pool import launch, runner, sandbox

from .test_runner import DEFINITION, POOL_TOKEN, Rig  # noqa: F401  (Rig for the fixture)
from .test_runner import rig  # noqa: F401

SRC = Path(__file__).resolve().parents[2] / "src" / "specflo"

# Writes down the namespaces of the process it runs as, then becomes what the
# rest of its command line names: the member's own pi double.
PROBE = '''import json, os, sys

out, rest = sys.argv[1], sys.argv[2:]
with open(out, "w", encoding="utf-8") as f:
    json.dump({name: os.readlink("/proc/self/ns/" + name)
               for name in ("mnt", "user", "pid", "net")}, f)
os.execv(rest[0], rest)
'''

# Leaves a process behind that writes a line every so often, then becomes the
# member's pi double. What the line goes to says whether it still runs.
BACKGROUND = '''import json, os, subprocess, sys

beat, rest = sys.argv[1], sys.argv[2:]
subprocess.Popen(
    [sys.executable, "-c",
     "import time, sys\\n"
     "f = open(sys.argv[1], 'a')\\n"
     "while True:\\n"
     "    f.write('.'); f.flush(); time.sleep(0.05)\\n",
     beat],
)
os.execv(rest[0], rest)
'''


def skip_without_a_sandbox() -> None:
    reason = sandbox.unavailable()
    if reason is not None:
        pytest.skip(f"no sandbox on this rig: {reason}")


def command_in_front(rig, source: str, name: str, first_arg: Path) -> str:
    """The rig's member command with a script of its own in front of it.

    The program stays the rig's own, so the sandbox binds back what it binds
    back for any member of this rig; the script goes in the working
    directory, which every member may read and write.
    """
    script = rig.work / name
    script.write_text(source, encoding="utf-8")
    program = shlex.split(rig.command)[0]
    return f"{program} {script} {first_arg} {rig.command}"


def written(path: Path, timeout: float = 10.0) -> dict[str, str]:
    """What the probe wrote, waited for: a start returns as soon as pi runs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
    raise AssertionError(f"the member never wrote {path.name}")


def here() -> dict[str, str]:
    """The namespaces this process, the daemon's stand-in, is in."""
    return {
        name: os.readlink(f"/proc/self/ns/{name}")
        for name in ("mnt", "user", "pid", "net")
    }


def test_a_started_member_is_in_its_own_mount_user_and_pid_namespaces(rig) -> None:
    skip_without_a_sandbox()
    from dataclasses import replace

    out = rig.work / "namespaces.json"
    member = replace(
        rig.local_member(), command=command_in_front(rig, PROBE, "probe.py", out)
    )

    rig.start(member)

    inside = written(out)
    daemon = here()
    for namespace in ("mnt", "user", "pid"):
        assert inside[namespace] != daemon[namespace], f"the member shares {namespace}"


def test_a_local_member_is_in_a_network_namespace_of_its_own(rig) -> None:
    skip_without_a_sandbox()
    from dataclasses import replace

    out = rig.work / "namespaces.json"
    member = replace(
        rig.local_member(), command=command_in_front(rig, PROBE, "probe.py", out)
    )

    rig.start(member)

    assert written(out)["net"] != here()["net"]


def test_a_hosted_member_keeps_the_host_network_and_is_sandboxed_all_the_same(rig) -> None:
    skip_without_a_sandbox()
    from dataclasses import replace

    out = rig.work / "namespaces.json"
    member = replace(
        rig.hosted_member(), command=command_in_front(rig, PROBE, "probe.py", out)
    )

    rig.start(member)

    inside = written(out)
    assert inside["net"] == here()["net"]
    assert inside["mnt"] != here()["mnt"]


def test_a_member_s_background_process_is_gone_once_the_lease_ends(rig) -> None:
    skip_without_a_sandbox()
    from dataclasses import replace

    beat = rig.work / "beat"
    member = replace(
        rig.local_member(),
        command=command_in_front(rig, BACKGROUND, "background.py", beat),
    )
    name = rig.start(member)
    deadline = time.monotonic() + 10.0
    while not beat.exists() or beat.stat().st_size == 0:
        assert time.monotonic() < deadline, "the member's background process never ran"
        time.sleep(0.05)

    runner.stop(name, "released", pool_token=POOL_TOKEN)

    time.sleep(0.5)
    settled = beat.stat().st_size
    time.sleep(0.5)
    assert beat.stat().st_size == settled, "the member's background process still runs"


# -- the one way to start a member -----------------------------------------


def _calls(path: Path) -> list[ast.Call]:
    return [n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(n, ast.Call)]


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def test_the_pi_command_line_is_built_for_a_start_in_one_place_only() -> None:
    sources = sorted(SRC.rglob("*.py"))
    assert sources, "sources not found"
    elsewhere = [
        f"{path.relative_to(SRC).as_posix()}:{call.lineno}"
        for path in sources
        if path.name != "launch.py"
        for call in _calls(path)
        if _called_name(call) == "pi_argv"
    ]
    assert elsewhere == [], "the pi command line is built outside the launch builder"


def test_the_one_builder_puts_the_sandbox_in_front_of_every_member(rig) -> None:
    for member in (rig.local_member(), rig.hosted_member()):
        argv = launch.member_argv(
            DEFINITION, member, dict(os.environ),
            cwd=rig.work, state_dir=rig.tmp_path, config_dir=None,
        )
        names = [Path(word).name for word in argv]
        # A scope of its own comes first when the user manager can make one.
        assert names[0] in ("env", "prlimit", "bwrap")
        bwrap = names.index("bwrap")
        separator = argv.index("--", bwrap)
        assert argv[separator + 1 :] == launch.pi_argv(DEFINITION, member)


def test_a_member_of_a_class_with_no_profile_is_refused_rather_than_started(rig) -> None:
    from dataclasses import replace

    member = replace(rig.local_member(), egress="shared-with-everyone")
    with pytest.raises(sandbox.UnknownProfile, match="shared-with-everyone"):
        launch.member_argv(
            DEFINITION, member, dict(os.environ), cwd=rig.work, state_dir=rig.tmp_path
        )
