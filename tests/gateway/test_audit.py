"""Each gateway run leaves one record in the daemon's audit log, and no secret goes anywhere.

A run is recorded when it ends, in the same ``audit.jsonl`` the daemon's
routes append to, with the forge, repo, issue, the id of the user whose
label started it, its start and end times and its outcome: success,
failure, timeout, no free agent or cancelled.

The forge credentials are files in the daemon root. With known fake strings
in a Gitea token file and a GitHub App key file, neither string shows in the
daemon's log, the audit file, a comment the bot posted, or what the member
is handed: its environment, its prompt and the files of its working dir.
"""

import asyncio
import dataclasses
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import pytest

from specflo.daemon import routes
from specflo.gateway import config, gitea, run, service, store

from .stub_forge import StubGitea

TOKEN = "fake-gitea-token-5ecre7-a1b2c3"
KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "FAKE-APP-KEY-9f8e7d6c5b4a-not-a-real-key\n"
    "-----END RSA PRIVATE KEY-----\n"
)
KEY_MARK = "FAKE-APP-KEY-9f8e7d6c5b4a"
SECRETS = (TOKEN, KEY_MARK)
REPO = "me/thing"
OPERATOR = 7
REPORT = "# Answer\n\nIt works.\n"

#: Every field a run record names.
RUN_FIELDS = {"forge", "repo", "issue", "actor_id", "start", "end", "outcome"}


def _secret_file(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "root"
    path.mkdir()
    return path


@dataclasses.dataclass
class Setup:
    stub: StubGitea
    forge: config.Forge
    github: config.Forge
    kind: service.ForgeKind
    #: The gateway's first start: before any label is put on.
    start: datetime


@pytest.fixture
def setup(root) -> Setup:
    stub = StubGitea(token=TOKEN)
    forge = config.Forge(
        name="home", kind=config.GITEA, url=stub.base_url, repos=(REPO,),
        operator_id=OPERATOR, pool="rfc-research",
        token_file=_secret_file(root, "gitea.token", TOKEN + "\n"),
    )
    # A GitHub forge in the same configuration: its key is a secret in the
    # daemon root all the same, though this service has no GitHub kind.
    github = config.Forge(
        name="hub", kind=config.GITHUB, url=config.GITHUB_API, repos=("me/other",),
        operator_id=OPERATOR, pool="rfc-research", app_id=42,
        key_file=_secret_file(root, "github.pem", KEY),
    )
    kind = dataclasses.replace(
        service.GITEA_KIND,
        client=lambda forge: gitea.GiteaClient(forge, transport=stub.transport()),
    )
    return Setup(stub, forge, github, kind, start=stub.now)


class StubPool:
    """A pool whose agent does what *agent* says in its working dir, recording
    what each member is handed: its environment, its prompt and its files."""

    def __init__(self, agent, *, free=True):
        self.agent = agent
        self.free = free
        self.handed = []
        self.leases = []

    async def lease(self, pool, *, cwd, label, env):
        if not self.free:
            raise run.NoFreeAgent(f"pool '{pool}' has no free member.")
        self.leases.append(Path(cwd))
        self.handed.append({"label": label, "env": dict(env)})
        return run.Leased(lease_id=f"lease-{len(self.leases)}", agent="research-1", token="t")

    async def prompt(self, leased, text, *, timeout):
        cwd = self.leases[int(leased.lease_id.rpartition("-")[2]) - 1]
        files = {
            str(path.relative_to(cwd)): path.read_bytes().decode("utf-8", "replace")
            for path in sorted(cwd.rglob("*")) if path.is_file()
        }
        self.handed.append({"prompt": text, "files": files})
        await self.agent(cwd)

    async def end(self, leased, cause):
        pass


async def reports(cwd):
    (cwd / run.REPORT_FILE).write_text(REPORT, encoding="utf-8")


async def fails(cwd):
    raise run.RunError("the agent's turn ended in an error.")


async def hangs(cwd):
    await asyncio.Event().wait()


async def clone(url, destination, *, token=None, **options):
    # The real clone keeps the token in git's environment; the stub makes the dir.
    Path(destination).mkdir()


def _service(setup, root, pool, **options):
    options.setdefault("lease_retry", 0.01)
    return service.GatewayService(
        store.open_store(root, now=setup.start), pool,
        kinds={config.GITEA: setup.kind}, clone=clone, clock=lambda: setup.stub.now,
        **options,
    )


def _config(setup):
    return config.GatewayConfig(path=None, forges=(setup.forge, setup.github), poll_every=60)


def _label(setup, number):
    setup.stub.tick()
    setup.stub.label(REPO, number, setup.forge.label, OPERATOR)


def _run_records(root):
    path = root / routes.AUDIT_FILENAME
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in records if r.get("operation") == service.RUN_OPERATION]


def _poll_and_join(setup, root, pool, **options):
    async def main():
        gateway = _service(setup, root, pool, **options)
        try:
            await gateway.poll(_config(setup))
            await gateway.join()
        finally:
            await gateway.aclose()

    asyncio.run(main())


def test_a_success_and_a_failure_leave_two_run_records_with_every_field(setup, root):
    setup.stub.add_issue(REPO, 1)
    setup.stub.add_issue(REPO, 2)
    _label(setup, 1)
    _label(setup, 2)
    done = iter([reports, fails])

    async def agent(cwd):
        await next(done)(cwd)

    _poll_and_join(setup, root, StubPool(agent))

    records = _run_records(root)
    assert len(records) == 2
    for record in records:
        assert RUN_FIELDS <= set(record)
        assert record["forge"] == "home" and record["repo"] == REPO
        assert record["actor_id"] == OPERATOR
        start = datetime.fromisoformat(record["start"])
        end = datetime.fromisoformat(record["end"])
        assert start.tzinfo is not None and start <= end
    by_issue = {record["issue"]: record["outcome"] for record in records}
    assert by_issue == {1: service.SUCCESS, 2: service.FAILURE}


@pytest.mark.parametrize(
    ("agent", "free", "outcome"),
    [
        (hangs, True, service.TIMEOUT),
        (reports, False, service.NO_FREE_AGENT),
    ],
)
def test_a_run_that_runs_out_of_time_or_finds_no_agent_is_recorded_so(
    setup, root, agent, free, outcome
):
    setup.stub.add_issue(REPO, 1)
    _label(setup, 1)

    _poll_and_join(setup, root, StubPool(agent, free=free), limit=0.05, lease_wait=0.05)

    (record,) = _run_records(root)
    assert record["outcome"] == outcome and record["issue"] == 1


def test_a_run_cancelled_when_the_daemon_stops_is_recorded_as_cancelled(setup, root):
    setup.stub.add_issue(REPO, 1)
    _label(setup, 1)
    pool = StubPool(hangs)

    async def main():
        gateway = _service(setup, root, pool)
        try:
            await gateway.poll(_config(setup))
            async with asyncio.timeout(5):
                while len(pool.handed) < 2:
                    await asyncio.sleep(0.01)
        finally:
            await gateway.aclose()

    asyncio.run(main())

    (record,) = _run_records(root)
    assert record["outcome"] == service.CANCELLED


def test_no_token_or_key_reaches_the_log_the_audit_the_comments_or_the_member(
    setup, root, caplog
):
    caplog.set_level(logging.DEBUG)
    setup.stub.add_issue(REPO, 1)
    setup.stub.add_issue(REPO, 2)
    _label(setup, 1)
    _label(setup, 2)
    done = iter([reports, fails])

    async def agent(cwd):
        await next(done)(cwd)

    pool = StubPool(agent)
    _poll_and_join(setup, root, pool)

    # The check bites: both runs happened, the member was handed something,
    # and the forge was reached with the token.
    assert len(_run_records(root)) == 2
    assert len(pool.handed) == 4
    assert any(TOKEN in str(seen.headers) for seen in setup.stub.requests)

    log = caplog.text + "\n".join(record.getMessage() for record in caplog.records)
    audit = (root / routes.AUDIT_FILENAME).read_text(encoding="utf-8")
    bodies = [
        json.dumps(seen.body) for seen in setup.stub.requests
        if seen.method in ("POST", "PATCH")
    ]
    assert bodies
    handed = json.dumps(pool.handed)
    for secret in SECRETS:
        assert secret not in log
        assert secret not in audit
        assert all(secret not in body for body in bodies)
        assert secret not in handed
