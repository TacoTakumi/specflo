"""``specflo lease request``: a member of a named pool, asked of the daemon.

An orchestrator asks the daemon that holds the pool for one member of it. The
daemon grants a lease on a free member, starts a fresh pi for it in the
working directory the request was made from, and answers with the lease id,
the agent's name and the lease token. The verb prints the first two, keeps the
token under the checkout's ``.specflo/leases/`` where the ``specflo agent``
verbs find it, and never prints it.

The daemon is a real one on a loopback port, started on a root that holds a
pool directory on disk. Its members are real processes: the stub pi under an
agent host, placed by a fake herdr, as in the pool service's tests.
"""

from __future__ import annotations

import json
import shutil
import stat
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import config
from specflo.cli import app
from specflo.daemon import auth
from specflo.daemon.app import create_app
from specflo.pool import cli_admin
from specflo.pool import config as pool_config
from specflo.service.pool_remote import LEASES_PATH

from .test_runner import pid_alive

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "pool"

REBASER = """\
---
role: Rebases the work branch and reports conflicts
tools: [read, bash]
egress: local
---

You are the rebaser.
"""


def write_pool(rig) -> None:
    """A pool directory under the rig's daemon root: one pool, "rebasers", of
    one local member that starts the rig's pi double."""
    directory = cli_admin.pool_dir(rig.root)
    folder = directory / pool_config.DEFINITIONS_DIR
    folder.mkdir(parents=True)
    (folder / "rebaser.md").write_text(REBASER, encoding="utf-8")
    shutil.copy(FIXTURES / "llama-swap.yaml", directory / "llama-swap.yaml")
    data = {
        "llama_swap": "llama-swap.yaml",
        "members": [{
            "name": "local-1", "command": rig.command, "backing": "local",
            "model": "model-a", "labels": [], "capacity": 1, "egress": "local",
        }],
        "pools": [{
            "name": "rebasers", "definition": "rebaser", "members": ["local-1"],
            "size": 1, "idle_default": "10m", "idle_max": "4h",
        }],
    }
    (directory / pool_config.POOL_FILE).write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def pool_daemon(pool_rig):
    """A real daemon on a free loopback port, on the rig's root with a pool in it."""
    import uvicorn

    write_pool(pool_rig)
    server = uvicorn.Server(
        uvicorn.Config(create_app(pool_rig.root), host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "the daemon did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield {
        "root": pool_rig.root,
        "url": f"http://127.0.0.1:{port}",
        "token": auth.mint_token(pool_rig.root, "developer"),
    }
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def checkout(tmp_path, monkeypatch, pool_daemon):
    """An orchestrator's checkout with the daemon registered, and the shell in it."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    config.init_config(checkout)
    monkeypatch.chdir(checkout)
    registered = runner.invoke(
        app, ["remote", "add", "home", pool_daemon["url"], "--token", pool_daemon["token"]]
    )
    assert registered.exit_code == 0, registered.output
    return checkout


def request(*args: str) -> dict:
    """One granted request, as the verb reports it with --json."""
    result = runner.invoke(app, ["lease", "request", *args, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def audit_records(root: Path) -> list[dict]:
    path = root / "audit.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# -- a grant ----------------------------------------------------------------


def test_a_request_prints_the_lease_and_the_agent_and_the_agent_answers(
    checkout, pool_rig, pool_daemon
):
    result = runner.invoke(app, ["lease", "request", "rebasers"])

    assert result.exit_code == 0, result.output
    with pool_rig.store() as store:
        (row,) = store.list_leases(state="active")
    assert row.id in result.stdout
    assert "local-1" in result.stdout
    assert row.member == "local-1"
    assert pid_alive(pool_rig.status("local-1")["pi_pid"])

    # the holder drives the member with the token the verb stored
    answered = runner.invoke(app, ["agent", "prompt", "local-1", "rebase the branch"])
    assert answered.exit_code == 0, answered.output
    assert "done" in answered.stdout


def test_the_token_is_stored_for_the_agent_verbs_and_never_printed(checkout, pool_rig):
    result = runner.invoke(app, ["lease", "request", "rebasers"])

    assert result.exit_code == 0, result.output
    token_file = checkout / ".specflo" / "leases" / "local-1.token"
    token = token_file.read_text(encoding="utf-8").strip()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert token not in result.output
    # the token is the lease's holder: the store keeps its hash
    with pool_rig.store() as store:
        (row,) = store.list_leases(state="active")
    assert row.holder_hash == auth.hash_token(token)
    # a checkout is a repository: what is under leases/ is never committed
    assert (token_file.parent / ".gitignore").read_text(encoding="utf-8") == "*\n"


def test_json_output_names_the_lease_and_the_agent_and_no_token(checkout):
    granted = request("rebasers")

    token = (checkout / ".specflo" / "leases" / "local-1.token").read_text().strip()
    assert granted["agent"] == "local-1"
    assert granted["lease"].startswith("lease-")
    assert (granted["pool"], granted["remote"]) == ("rebasers", "home")
    assert token not in json.dumps(granted)


def test_without_the_token_file_the_member_refuses_the_prompt(checkout):
    request("rebasers")
    (checkout / ".specflo" / "leases" / "local-1.token").unlink()

    refused = runner.invoke(app, ["agent", "prompt", "local-1", "rebase the branch"])

    assert refused.exit_code != 0


# -- the working directory --------------------------------------------------


def test_the_member_starts_in_the_directory_the_request_was_made_from(
    checkout, pool_rig, monkeypatch
):
    below = checkout / "src"
    below.mkdir()
    monkeypatch.chdir(below)

    request("rebasers")

    assert pool_rig.recorded()["cwd"] == str(below)
    # the token is the checkout's, found upward from where the verbs run
    assert (checkout / ".specflo" / "leases" / "local-1.token").is_file()


def test_the_member_starts_in_the_directory_the_request_names(checkout, pool_rig):
    request("rebasers", "--cwd", str(pool_rig.work))

    assert pool_rig.recorded()["cwd"] == str(pool_rig.work)


def test_a_relative_cwd_is_taken_from_where_the_request_was_made(checkout, pool_rig):
    (checkout / "pieces").mkdir()

    request("rebasers", "--cwd", "pieces")

    assert pool_rig.recorded()["cwd"] == str(checkout / "pieces")


def test_a_working_directory_that_is_not_there_is_refused(checkout, pool_rig):
    result = runner.invoke(app, ["lease", "request", "rebasers", "--cwd", "nowhere"])

    assert result.exit_code != 0
    assert "nowhere" in result.output
    assert pool_rig.pane_names() == []


# -- refusals ---------------------------------------------------------------


def test_an_unknown_pool_exits_non_zero_naming_it(checkout, pool_rig):
    result = runner.invoke(app, ["lease", "request", "reviewers"])

    assert result.exit_code != 0
    assert "reviewers" in result.output
    assert pool_rig.pane_names() == []
    assert not (checkout / ".specflo" / "leases" / "local-1.token").exists()


def test_an_idle_limit_above_the_pools_maximum_exits_non_zero_naming_the_maximum(
    checkout, pool_rig
):
    result = runner.invoke(app, ["lease", "request", "rebasers", "--idle-limit", "5h"])

    assert result.exit_code != 0
    assert "4h" in result.output
    assert pool_rig.pane_names() == []
    with pool_rig.store() as store:
        assert store.list_leases() == []


def test_without_an_idle_limit_the_pools_default_is_recorded(checkout, pool_rig):
    granted = request("rebasers")

    with pool_rig.store() as store:
        assert store.get_lease(granted["lease"]).idle_limit == 600


def test_an_idle_limit_up_to_the_maximum_is_recorded(checkout, pool_rig):
    granted = request("rebasers", "--idle-limit", "90m")

    with pool_rig.store() as store:
        assert store.get_lease(granted["lease"]).idle_limit == 90 * 60


def test_an_idle_limit_that_is_no_duration_is_refused_before_the_daemon_is_asked(
    checkout, pool_rig
):
    result = runner.invoke(app, ["lease", "request", "rebasers", "--idle-limit", "soon"])

    assert result.exit_code != 0
    assert "soon" in result.output
    assert pool_rig.pane_names() == []


def test_a_pool_with_no_free_member_is_refused_and_the_held_token_is_kept(checkout, pool_rig):
    request("rebasers")
    token_file = checkout / ".specflo" / "leases" / "local-1.token"
    held = token_file.read_text()

    result = runner.invoke(app, ["lease", "request", "rebasers", "--wait", "0"])

    assert result.exit_code != 0
    assert "rebasers" in result.output
    assert token_file.read_text() == held


# -- the route --------------------------------------------------------------


def test_the_route_needs_a_bearer_token(pool_daemon, pool_rig):
    client = TestClient(create_app(pool_daemon["root"]))
    body = {"pool": "rebasers", "cwd": str(pool_rig.work)}

    assert client.post(LEASES_PATH, json=body).status_code == 401
    wrong = client.post(LEASES_PATH, json=body, headers={"Authorization": "Bearer not-one"})
    assert wrong.status_code == 401
    assert pool_rig.pane_names() == []
    with pool_rig.store() as store:
        assert store.list_leases() == []


def test_a_grant_is_audited_with_the_acting_identity_and_a_refusal_is_not(
    checkout, pool_daemon, pool_rig
):
    runner.invoke(app, ["lease", "request", "reviewers"])
    assert audit_records(pool_daemon["root"]) == []

    granted = request("rebasers")

    (record,) = audit_records(pool_daemon["root"])
    assert record["identity"] == "developer"
    assert record["operation"] == "lease_request"
    assert record["id"] == granted["lease"]
    # the record, like the store, never holds the token
    token = (checkout / ".specflo" / "leases" / "local-1.token").read_text().strip()
    assert token not in json.dumps(record)


def test_the_lease_row_says_who_asked(checkout, pool_rig):
    granted = request("rebasers", "--label", "orchestrator-a")

    with pool_rig.store() as store:
        label = store.get_lease(granted["lease"]).holder_label
    assert "orchestrator-a" in label
    assert "developer" in label


def test_the_route_refuses_a_body_it_does_not_know(pool_daemon, pool_rig):
    client = TestClient(create_app(pool_daemon["root"]))
    client.headers["Authorization"] = f"Bearer {pool_daemon['token']}"
    good = {"pool": "rebasers", "cwd": str(pool_rig.work)}

    for body in (
        {"cwd": str(pool_rig.work)},
        {**good, "idle_limit": "5h"},
        {**good, "idle_limit": True},
        {**good, "team": "gamedev"},
        {**good, "cwd": "relative/dir"},
    ):
        response = client.post(LEASES_PATH, json=body)
        assert response.status_code in (400, 422), (body, response.text)
    assert pool_rig.pane_names() == []


def test_a_daemon_root_with_no_pool_directory_starts_and_refuses_a_request(tmp_path):
    from specflo import daemon

    root = daemon.prepare_root(tmp_path / "plain")
    client = TestClient(create_app(root))
    client.headers["Authorization"] = f"Bearer {auth.mint_token(root, 'developer')}"

    assert client.get("/whoami").status_code == 200
    response = client.post(LEASES_PATH, json={"pool": "rebasers", "cwd": str(tmp_path)})

    assert response.status_code == 400
    assert "pool" in response.json()["detail"].lower()
