"""A member started in a scope lives and ends as a member always has.

The scope command runs the member in place, so the agent host still starts
it, talks to it over the same pipes and stops it the same way. When the lease
ends, the member's scope goes with it, and so does every process that ran in
it, one the member left running in the background included.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from typer.testing import CliRunner

from specflo.agent.cli import agent_app
from specflo.agent.statefiles import AgentPaths, read_status
from specflo.pool import runner, sandbox

from .test_runner import LEASE_TOKEN, POOL_TOKEN, pid_alive, rig, wait_until  # noqa: F401
from .test_sandbox_scope import skip_without_a_scope


def scope_of(pid: int) -> str:
    return Path(f"/proc/{pid}/cgroup").read_text().strip().split(":", 2)[2]


def processes_in(scope: str) -> list[int]:
    try:
        text = Path(f"/sys/fs/cgroup{scope}/cgroup.procs").read_text()
    except OSError:
        return []
    return [int(pid) for pid in text.split()]


def test_a_scoped_member_answers_a_prompt_and_leaves_nothing_of_its_scope(rig):
    skip_without_a_scope()
    # The member leaves a process of its own running in the background: it
    # starts one in a session of its own, then goes on as the rig's pi does.
    program, recorder, *rest = rig.command.split(" ")
    leaving = Path(recorder).with_name("recorder_leaving_a_process.py")
    leaving.write_text(
        "import os, subprocess, sys\n"
        "subprocess.Popen(['sleep', '300'], start_new_session=True,\n"
        "                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "                 stderr=subprocess.DEVNULL)\n"
        f"os.execv(sys.executable, [sys.executable, {recorder!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    member = replace(rig.hosted_member(), command=" ".join([program, str(leaving), *rest]))
    name = rig.start(member)
    started = read_status(AgentPaths.resolve(name).status)["pi_pid"]
    # The host's child is the launcher until it becomes the scope command.
    assert wait_until(lambda: scope_of(started) != scope_of(os.getpid()))
    scope = scope_of(started)
    assert scope.endswith(".scope") and "/app.slice/" in scope
    limit = Path(f"/sys/fs/cgroup{scope}/pids.max").read_text().strip()
    assert limit == str(sandbox.DEFAULT_LIMITS.max_procs)

    answer = CliRunner().invoke(
        agent_app, ["prompt", name, "hello", "--lease-token", LEASE_TOKEN]
    )

    assert answer.exit_code == 0, answer.output
    assert "done" in answer.output
    ran = processes_in(scope)
    assert len(ran) >= 2, ran

    runner.stop(name, "released", pool_token=POOL_TOKEN)

    assert wait_until(lambda: not Path(f"/sys/fs/cgroup{scope}").exists(), timeout=15)
    assert [pid for pid in ran if pid_alive(pid)] == []
