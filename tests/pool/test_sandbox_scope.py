"""Each member in a systemd scope of its own, with the member task limit.

The per-uid process limit counts every thread the user runs on the host, so a
member's room shrinks as the desktop and the other members grow, and one
member's fork loop eats the room of all of them. A scope counts only what
runs in it. When the user manager can be reached, each member starts in a
scope of its own whose task limit is the member process limit, and prlimit
sets no process count. The scope command runs its command in place, so the
member's pid chain is as it was.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from specflo.pool import launch, sandbox
from specflo.pool.config import Member

from .test_runner import DEFINITION
from .test_sandbox_checkout_secrets import skip_without_a_sandbox

LIMIT = 40


def skip_without_a_scope() -> None:
    skip_without_a_sandbox()
    reason = sandbox.scope_fault(os.environ)
    if reason is not None:
        pytest.skip(f"no user manager to make a scope with: {reason}")


@pytest.fixture
def environ(tmp_path) -> dict[str, str]:
    """The daemon's environment: a home of its own, and the session's runtime
    directory, through which the user manager is reached."""
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home), "PATH": os.environ["PATH"],
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    }


def member_command(
    tmp_path: Path, environ, name: str, script: str,
    limits: sandbox.Limits = sandbox.Limits(max_procs=LIMIT),
) -> tuple[list[str], Path]:
    """The whole command a member *name* running python *script* starts as, and
    where its script writes."""
    work = tmp_path / name
    work.mkdir()
    (work / "probe.py").write_text(script, encoding="utf-8")
    out = work / "found.txt"
    member = Member(
        name=name, backing="hosted", labels=(), capacity=1, egress="no-train",
        model="some-vendor/some-model", account="team-a",
        command=f"{sys.executable} {work / 'probe.py'} {out}",
    )
    command = launch.member_argv(
        DEFINITION, member, environ, cwd=work, state_dir=tmp_path / "state",
        limits=limits,
    )
    return command, out


SLEEP = '''import sys, time
open(sys.argv[1], "w").write("up")
time.sleep(30)
'''

FORK_ONCE = '''import os, sys
try:
    pid = os.fork()
except OSError as exc:
    open(sys.argv[1], "w").write(f"refused: {exc}")
    raise SystemExit(0)
if pid == 0:
    os._exit(0)
os.waitpid(pid, 0)
open(sys.argv[1], "w").write("forked")
'''

FORK_LOOP = '''import os, sys, time
kids = []
while True:
    try:
        pid = os.fork()
    except OSError:
        break
    if pid == 0:
        time.sleep(30)
        os._exit(0)
    kids.append(pid)
open(sys.argv[1], "w").write(str(len(kids)))
time.sleep(30)
'''


def test_the_member_argv_is_the_scope_then_prlimit_without_a_process_count_then_bwrap(
    tmp_path, environ
):
    skip_without_a_scope()

    # A processor-time limit too, so prlimit has something left to set.
    command, _ = member_command(
        tmp_path, environ, "hosted-1", SLEEP,
        limits=sandbox.Limits(max_procs=LIMIT, cpu_seconds=3600),
    )

    names = [Path(word).name for word in command]
    run = names.index("systemd-run")
    assert command[run + 1:run + 7] == [
        "--user", "--scope", "--quiet", "-p", f"TasksMax={LIMIT}", "--",
    ]
    prlimit = names.index("prlimit")
    bwrap = names.index("bwrap")
    assert run < prlimit < bwrap
    assert not any(word.startswith("--nproc") for word in command[prlimit:bwrap])


def test_a_started_member_runs_in_a_scope_of_its_own_with_the_limit(tmp_path, environ):
    skip_without_a_scope()
    command, out = member_command(tmp_path, environ, "hosted-1", SLEEP)

    # The scope command, prlimit and bwrap each run the next in place, so the
    # started process is the member's sandbox, and its cgroup is read from here.
    member = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while not out.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        path = Path(f"/proc/{member.pid}/cgroup").read_text().strip().split(":", 2)[2]
        limit = Path(f"/sys/fs/cgroup{path}/pids.max").read_text().strip()
    finally:
        member.kill()
        member.wait()

    assert "/user@" in path and "/app.slice/" in path and path.endswith(".scope")
    assert path != Path("/proc/self/cgroup").read_text().strip().split(":", 2)[2]
    assert limit == str(LIMIT)


def test_a_member_still_forks_after_the_host_grows_by_a_thousand_threads(tmp_path, environ):
    skip_without_a_scope()
    command, out = member_command(tmp_path, environ, "hosted-1", FORK_ONCE)
    host = subprocess.Popen([sys.executable, "-c", (
        "import threading, time\n"
        "gate = threading.Event()\n"
        "for _ in range(1000):\n"
        "    threading.Thread(target=gate.wait, daemon=True).start()\n"
        "print('up', flush=True)\n"
        "time.sleep(60)\n"
    )], stdout=subprocess.PIPE, text=True)
    try:
        assert host.stdout.readline().strip() == "up"

        done = subprocess.run(command, capture_output=True, text=True, timeout=60)

        assert done.returncode == 0, done.stderr
        assert out.read_text(encoding="utf-8") == "forked"
    finally:
        host.kill()
        host.wait()


def test_a_fork_loop_in_one_member_stops_at_its_limit_and_another_member_still_forks(
    tmp_path, environ
):
    skip_without_a_scope()
    loop, loop_out = member_command(tmp_path, environ, "hosted-1", FORK_LOOP)
    other, other_out = member_command(tmp_path, environ, "hosted-2", FORK_ONCE)
    first = subprocess.Popen(loop, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while not loop_out.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert loop_out.exists(), "the fork loop never stopped"
        assert int(loop_out.read_text(encoding="utf-8")) < LIMIT

        done = subprocess.run(other, capture_output=True, text=True, timeout=60)

        assert done.returncode == 0, done.stderr
        assert other_out.read_text(encoding="utf-8") == "forked"
    finally:
        first.kill()
        first.wait()
