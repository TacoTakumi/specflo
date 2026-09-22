"""A worker and critic team, end to end: leased, led through one round, given back.

The test plays the orchestrator and uses what an orchestrator has: the lease
verbs and the ``specflo agent`` verbs, run from a checkout with the daemon
registered. It leases the team "review", hands the worker a task, reads the
result, hands the result to the critic, reads the critique, hands the critique
to the worker for one revise round, and releases the team. The pool gives the
members no way to reach each other, so every text goes through the test, and
what each member's pi was sent shows that it did.

The pool directory is the one ``specflo serve pool init`` lays down, so the
worker and the critic are the definitions that ship, byte for byte. Around
them the test supplies what an admin would: a pool file with one local member
to each pool, the fixture llama-swap configuration, whose models "model-a" and
"model-c" may be loaded side by side, and the team file. Each member is a stub
pi with a reply of its own, behind a recorder of its own, under a real agent
host that a fake herdr places. Nothing here needs the rig, a GPU or a network.

When the team is released nothing of it may stay: no active lease in the
daemon's pool store, no member process, and no lease token under the checkout.
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path

import pytest
import yaml

from specflo.cli import app
from specflo.pool import cli_admin, definitions
from specflo.pool import config as pool_config
from specflo.pool.teams import TEAMS_DIR

from . import test_lease_request
from .test_lease_request import FIXTURES, checkout, runner  # noqa: F401  (fixtures)
from .test_lease_request import pool_daemon as plain_daemon  # noqa: F401  (fixture)
from .test_runner import pid_alive, wait_until
from .test_team_lease import active

TASK = "Add a --dry-run flag to the sync command and a test for it."
DRAFT = "Added --dry-run to sync.py; tests/test_sync.py passes."
CRITIQUE = "sync.py:41: --dry-run still writes the lock file. Fix that first."

# role -> (member, pool, shipped definition, llama-swap model, labels, its pi's reply)
CAST = {
    # the shipped worker needs a member that carries the label "code"
    "worker": ("coder-local", "workers", "worker", "model-a", ["code"], DRAFT),
    "critic": ("judge-local", "critics", "critic", "model-c", [], CRITIQUE),
}


def record_name(member: str) -> str:
    """What the recorder calls the file it writes for *member*.

    It is a name and not a path: a member runs inside a sandbox and writes
    in the working directory of its lease, which is the one place it may.
    """
    return f"record-{member}.json"


def capture_name(member: str) -> str:
    """What *member*'s pi calls the file it writes every frame it is sent to."""
    return f"capture-{member}.jsonl"


def written_by(rig, name: str) -> Path | None:
    """Where a member wrote *name*, wherever its lease ran, or None."""
    found = [p for p in rig.tmp_path.rglob(name) if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def member_command(rig, member: str, reply: str) -> str:
    """The rig's pi double, with a reply, a record and a capture of *member*'s own.

    The stub recalls: a reply names the prompts the same session had before
    it, so a revised result differs from the first and shows one context."""
    scenario = rig.tmp_path / f"scenario-{member}.json"
    scenario.write_text(
        json.dumps({"reply": reply, "recall": True, "capture": capture_name(member)}),
        encoding="utf-8",
    )
    recorder = rig.tmp_path / "recorder.py"
    stub = rig.tmp_path / "stub_pi.py"
    harness = shlex.split(rig.command)[0]
    return f"{harness} {recorder} {record_name(member)} {stub} {scenario}"


def write_shipped_team_pool(rig) -> None:
    """The pool directory ``init`` writes under the rig's daemon root, and on it
    a pool to each of the shipped worker and critic, and the team "review"."""
    laid = runner.invoke(app, ["serve", "--root", str(rig.root), "pool", "init"])
    assert laid.exit_code == 0, laid.output
    directory = cli_admin.pool_dir(rig.root)
    shutil.copy(FIXTURES / "llama-swap.yaml", directory / "llama-swap.yaml")
    data = {
        "llama_swap": "llama-swap.yaml",
        "models_file": str(rig.models_file),
        "members": [
            {
                "name": member, "command": member_command(rig, member, reply),
                "backing": "local", "model": model, "labels": labels,
                "capacity": 1, "egress": "local",
            }
            for member, _, _, model, labels, reply in CAST.values()
        ],
        "pools": [
            {
                "name": pool, "definition": definition, "members": [member],
                "size": 1, "idle_default": "10m", "idle_max": "4h",
            }
            for member, pool, definition, _, _, _ in CAST.values()
        ],
    }
    (directory / pool_config.POOL_FILE).write_text(yaml.safe_dump(data), encoding="utf-8")
    roles = [{"name": role, "pool": cast[1], "count": 1} for role, cast in CAST.items()]
    # init makes no teams directory: a team is the admin's to declare
    (directory / TEAMS_DIR).mkdir()
    (directory / TEAMS_DIR / "review.md").write_text(
        "---\n" + yaml.safe_dump({"roles": roles}) + "---\n\nOne works, one judges.\n",
        encoding="utf-8",
    )


@pytest.fixture
def shipped_team_pool(monkeypatch):
    """The pool directory the request verb's daemon is started on is the one above."""
    monkeypatch.setattr(test_lease_request, "write_pool", write_shipped_team_pool)


@pytest.fixture
def pool_daemon(shipped_team_pool, plain_daemon):
    """The request verb's daemon, under the name its checkout asks for it by."""
    return plain_daemon


def agent(*args: str) -> str:
    """One ``specflo agent`` verb, run from the checkout; what it printed."""
    result = runner.invoke(app, ["agent", *args])
    assert result.exit_code == 0, result.output
    return result.stdout.strip()


def prompts_sent_to(rig, member: str) -> list[str]:
    """The text of every prompt *member*'s pi was sent, in order."""
    capture = written_by(rig, capture_name(member))
    assert capture is not None, f"the pi of {member} was sent nothing"
    lines = capture.read_text(encoding="utf-8").splitlines()
    frames = [json.loads(line) for line in lines if line]
    return [frame["message"] for frame in frames if frame["type"] == "prompt"]


def started_with(rig, member: str) -> list[str]:
    """The command line *member*'s pi was started with."""
    assert wait_until(lambda: written_by(rig, record_name(member)) is not None), (
        f"the pi of {member} never started"
    )
    record = written_by(rig, record_name(member))
    assert wait_until(lambda: record.read_text(encoding="utf-8").endswith("}"))
    return json.loads(record.read_text(encoding="utf-8"))["argv"]


def token_files(checkout_dir: Path) -> list[Path]:
    return sorted(checkout_dir.rglob("*.token"))


def test_a_leased_worker_and_critic_do_one_work_critique_and_revise_round_and_nothing_stays(
    checkout, pool_rig, pool_daemon
):
    directory = cli_admin.pool_dir(pool_daemon["root"])

    # -- the team is leased: one id, an agent and a token to each role --------
    asked = runner.invoke(app, ["lease", "request", "--team", "review", "--wait", "0", "--json"])
    assert asked.exit_code == 0, asked.output
    granted = json.loads(asked.stdout)

    assert granted["team_lease"].startswith("team-")
    assert [(m["role"], m["pool"], m["agent"]) for m in granted["members"]] == [
        (role, cast[1], cast[0]) for role, cast in CAST.items()
    ]
    worker, critic = (m["agent"] for m in granted["members"])
    rows = active(pool_rig)
    assert sorted(row.id for row in rows) == sorted(m["lease"] for m in granted["members"])
    assert {row.team_lease_id for row in rows} == {granted["team_lease"]}
    assert [path.name for path in token_files(checkout)] == sorted(
        f"{m['agent']}.token" for m in granted["members"]
    )
    # each member is a process of its own, in the role the shipped file gives
    pids = []
    for member, _, name, _, _, _ in CAST.values():
        status = pool_rig.status(member)
        pids += [status["pi_pid"], status["host_pid"]]
        shipped = cli_admin.SHIPPED_DIR / f"{name}.md"
        laid = directory / pool_config.DEFINITIONS_DIR / f"{name}.md"
        assert laid.read_bytes() == shipped.read_bytes()
        definition = definitions.load_definition(shipped)
        argv = started_with(pool_rig, member)
        assert argv[argv.index("--append-system-prompt") + 1] == definition.prompt
        assert argv[argv.index("--tools") + 1] == ",".join(definition.tools)
    assert len(set(pids)) == 4 and all(pid_alive(pid) for pid in pids)

    # -- the round, by the agent verbs alone; the test carries every text -----
    assert DRAFT in agent("prompt", worker, TASK)
    result = agent("last", worker)
    assert DRAFT in result

    assert CRITIQUE in agent("prompt", critic, f"The task: {TASK}\nThe result: {result}")
    critique = agent("last", critic)
    assert CRITIQUE in critique

    revised = agent("prompt", worker, f"A critic found this. Revise.\n{critique}")
    assert revised == agent("last", worker)
    # the same worker revised, with the task still in its context
    assert DRAFT in revised and TASK in revised and revised != result

    # what reached each pi is what the orchestrator relayed, and nothing else
    (judged,) = prompts_sent_to(pool_rig, critic)
    assert result in judged and TASK in judged
    first, second = prompts_sent_to(pool_rig, worker)
    assert first == TASK
    assert critique in second

    # -- the team is given back as one -----------------------------------------
    released = runner.invoke(app, ["lease", "release", granted["team_lease"], "--json"])
    assert released.exit_code == 0, released.output
    assert json.loads(released.stdout) == {"lease": granted["team_lease"], "state": "released"}

    # -- and nothing of it stays -------------------------------------------------
    assert active(pool_rig) == []
    with pool_rig.store() as store:
        assert [lease.state for lease in store.list_leases()] == ["released", "released"]
    assert wait_until(lambda: not any(pid_alive(pid) for pid in pids))
    assert wait_until(lambda: pool_rig.pane_names() == [])
    assert token_files(checkout) == []
    held = runner.invoke(app, ["lease", "list", "--json"])
    assert held.exit_code == 0, held.output
    assert json.loads(held.stdout) == []
    # the members are gone for the former holder too, and it is told why
    gone = runner.invoke(app, ["agent", "prompt", worker, TASK])
    assert gone.exit_code == 12, gone.output
    assert "lease released" in gone.output
