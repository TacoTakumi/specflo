"""One research run: a lease, one prompt with a time limit, and the report.

A run leases a research agent from the forge's pool with the run's scratch
dir as the working directory, sends it one prompt and waits for it, no longer
than the run's limit, and ends the lease however the run ends: with the
report, with a failure, at the limit, or cancelled. The member's environment
points awiki at the scratch dir's wiki configuration and holds nothing of the
forge's credentials. The report is report.md in the scratch dir; one that is
missing, empty or not a plain file fails the run.

The pool here is a stub that records what it is asked. The adapter onto the
daemon's pool service is checked with a stub service and a stub agent host.
"""

import asyncio
import os
import time
from pathlib import Path

import pytest

from specflo.errors import SpecfloError
from specflo.gateway import run, scratch
from specflo.gateway.config import Forge

TOKEN = "fake-forge-token-77ab"
TOKEN_FILE = "gitea-bot-token"
KEY_FILE = "github-app-key.pem"
REPORT = "# Answer\n\nIt does what it says.\n"


def _forge(root: Path, kind: str = "gitea") -> Forge:
    if kind == "gitea":
        return Forge(
            name="home", kind="gitea", url="https://forge.example", repos=("rob/specflo",),
            operator_id=1, pool="research", token_file=root / TOKEN_FILE,
        )
    return Forge(
        name="hub", kind="github", url="https://api.github.com", repos=("rob/specflo",),
        operator_id=1, pool="research", app_id=42, key_file=root / KEY_FILE,
    )


class StubPool:
    """A pool that records each call, and whose agent does what *agent* says."""

    def __init__(self, agent=None, *, refuse=None, hold_lease=None):
        self.agent = agent or write_report(REPORT)
        self.refuse = refuse
        self.hold_lease = hold_lease
        self.leases = []
        self.prompts = []
        self.ended = []
        self.ended_event = asyncio.Event()
        self.prompt_cancelled = False

    async def lease(self, pool, *, cwd, label, env):
        self.leases.append({"pool": pool, "cwd": Path(cwd), "label": label, "env": dict(env)})
        if self.hold_lease is not None:
            await self.hold_lease.wait()
        if self.refuse is not None:
            raise self.refuse
        return run.Leased(lease_id="lease-1", agent="research-1", token="lease-token")

    async def prompt(self, leased, text, *, timeout):
        self.prompts.append({"lease": leased.lease_id, "text": text, "timeout": timeout})
        try:
            await self.agent(self.leases[-1]["cwd"])
        except asyncio.CancelledError:
            self.prompt_cancelled = True
            raise

    async def end(self, leased, cause):
        self.ended.append((leased.lease_id, cause))
        self.ended_event.set()


def write_report(text):
    async def agent(cwd):
        (cwd / run.REPORT_FILE).write_text(text, encoding="utf-8")
    return agent


async def never_finish(cwd):
    await asyncio.Event().wait()


async def write_nothing(cwd):
    pass


def _run(pool, forge, **options):
    """Run one research run in a fresh scratch dir; its result, or the fault it raised."""

    async def main():
        async with scratch.scratch_dir() as directory:
            (directory / scratch.INPUT_FILE).write_text("# The issue\n", encoding="utf-8")
            scratch.write_wiki_config(directory, "https://wiki.example", "fake-wiki-reader")
            try:
                return directory, await run.research(pool, forge, directory, **options)
            except run.RunError as exc:
                return directory, exc

    return asyncio.run(main())


def test_a_run_leases_in_the_scratch_dir_sends_one_prompt_and_returns_the_report(tmp_path):
    pool = StubPool()
    directory, report = _run(pool, _forge(tmp_path), label="home rob/specflo#7")

    assert report == REPORT
    assert pool.leases == [{
        "pool": "research", "cwd": directory, "label": "home rob/specflo#7",
        "env": scratch.wiki_environment(directory),
    }]
    assert len(pool.prompts) == 1
    text = pool.prompts[0]["text"]
    assert scratch.INPUT_FILE in text and run.REPORT_FILE in text
    assert str(directory) in text
    assert pool.prompts[0]["timeout"] == run.RUN_LIMIT
    assert pool.ended == [("lease-1", run.FINISHED)]


def test_the_default_limit_is_thirty_minutes():
    assert run.RUN_LIMIT == 30 * 60


def test_the_prompt_tells_the_agent_where_its_wiki_configuration_lies(tmp_path):
    pool = StubPool()
    directory, _ = _run(pool, _forge(tmp_path))

    text = pool.prompts[0]["text"]
    for name, value in scratch.wiki_environment(directory).items():
        assert f"{name}={value}" in text


@pytest.mark.parametrize("kind", ["gitea", "github"])
def test_the_member_is_handed_no_forge_credential_name_or_value(tmp_path, kind, monkeypatch):
    # The daemon's own environment holds the token, as a careless operator's might.
    monkeypatch.setenv("GITEA_TOKEN", TOKEN)
    forge = _forge(tmp_path, kind)
    pool = StubPool()
    directory, report = _run(pool, forge)

    assert report == REPORT
    handed = [pool.leases[0]["env"], pool.prompts[0]["text"]]
    env = pool.leases[0]["env"]
    words = [*env, *env.values(), pool.prompts[0]["text"]]
    for secret in (TOKEN, TOKEN_FILE, KEY_FILE, str(tmp_path), "GITEA_TOKEN"):
        assert not [word for word in words if secret in word], (secret, handed)


def test_an_agent_that_never_finishes_is_cut_off_at_the_limit_and_its_lease_ended(tmp_path):
    pool = StubPool(never_finish)
    started = time.monotonic()
    _, fault = _run(pool, _forge(tmp_path), limit=1)
    elapsed = time.monotonic() - started

    assert isinstance(fault, run.RunError)
    assert "did not finish in 1 seconds" in str(fault)
    assert elapsed < 10
    assert pool.prompt_cancelled
    assert pool.prompts[0]["timeout"] == 1
    assert pool.ended == [("lease-1", run.TIMED_OUT)]


def test_an_agent_that_writes_no_report_fails_the_run(tmp_path):
    pool = StubPool(write_nothing)
    _, fault = _run(pool, _forge(tmp_path))

    assert isinstance(fault, run.RunError)
    assert "no report.md" in str(fault)
    assert pool.ended == [("lease-1", run.FINISHED)]


@pytest.mark.parametrize("text", ["", "  \n\n\t\n"])
def test_an_empty_report_fails_the_run(tmp_path, text):
    pool = StubPool(write_report(text))
    _, fault = _run(pool, _forge(tmp_path))

    assert isinstance(fault, run.RunError)
    assert "empty" in str(fault)
    assert pool.ended == [("lease-1", run.FINISHED)]


def test_a_report_that_is_a_link_is_not_followed(tmp_path):
    outside = tmp_path / "daemon-secret"
    outside.write_text(TOKEN, encoding="utf-8")

    async def link(cwd):
        os.symlink(outside, cwd / run.REPORT_FILE)

    pool = StubPool(link)
    _, fault = _run(pool, _forge(tmp_path))

    assert isinstance(fault, run.RunError)
    assert TOKEN not in str(fault)
    assert pool.ended == [("lease-1", run.FINISHED)]


def test_a_report_that_is_a_dir_fails_the_run(tmp_path):
    async def folder(cwd):
        (cwd / run.REPORT_FILE).mkdir()

    _, fault = _run(StubPool(folder), _forge(tmp_path))

    assert isinstance(fault, run.RunError)
    assert "not a regular file" in str(fault)


def test_a_report_above_the_size_limit_fails_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "REPORT_MAX", 10)
    _, fault = _run(StubPool(write_report("x" * 11)), _forge(tmp_path))

    assert isinstance(fault, run.RunError)
    assert "larger than" in str(fault)


def test_an_agent_that_fails_ends_its_lease_and_the_fault_stands(tmp_path):
    async def fail(cwd):
        raise run.RunError("the agent's turn ended in an error: provider refused")

    pool = StubPool(fail)
    _, fault = _run(pool, _forge(tmp_path))

    assert isinstance(fault, run.RunError)
    assert "provider refused" in str(fault)
    assert pool.ended == [("lease-1", run.FAILED)]


def test_a_pool_that_refuses_fails_the_run_and_ends_nothing(tmp_path):
    pool = StubPool(refuse=SpecfloError("pool 'research' has no free member."))
    _, fault = _run(pool, _forge(tmp_path))

    assert isinstance(fault, run.RunError)
    assert "no free member" in str(fault)
    assert pool.prompts == []
    assert pool.ended == []


def test_a_cancelled_run_ends_its_lease(tmp_path):
    pool = StubPool(never_finish)

    async def main():
        async with scratch.scratch_dir() as directory:
            task = asyncio.create_task(run.research(pool, _forge(tmp_path), directory))
            while not pool.prompts:
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(main())
    assert pool.prompt_cancelled
    assert pool.ended == [("lease-1", run.CANCELLED)]


def test_a_run_cancelled_while_its_lease_is_granted_ends_the_lease_when_it_comes(tmp_path):
    async def main():
        hold = asyncio.Event()
        pool = StubPool(hold_lease=hold)
        async with scratch.scratch_dir() as directory:
            task = asyncio.create_task(run.research(pool, _forge(tmp_path), directory))
            while not pool.leases:
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert pool.ended == []
            hold.set()
            await asyncio.wait_for(pool.ended_event.wait(), 5)
        return pool

    pool = asyncio.run(main())
    assert pool.prompts == []
    assert pool.ended == [("lease-1", run.CANCELLED)]


def test_an_ending_that_fails_is_logged_and_the_report_still_read(tmp_path, caplog):
    pool = StubPool()

    async def end(leased, cause):
        pool.ended.append((leased.lease_id, cause))
        raise SpecfloError("member 'research-1' did not stop")

    pool.end = end
    _, report = _run(pool, _forge(tmp_path))

    assert report == REPORT
    assert pool.ended == [("lease-1", run.FINISHED)]
    assert "did not stop" in caplog.text


# --- the adapter onto the daemon's pool service ------------------------------


class StubGrant:
    def __init__(self, lease_id, agent, token):
        self.lease_id, self.agent, self.token = lease_id, agent, token


class StubService:
    """The two calls of the pool service the adapter makes, recorded."""

    def __init__(self, refuse=None):
        self.refuse = refuse
        self.grants = []
        self.endings = []

    def grant(self, pool_name, *, holder_label, cwd):
        self.grants.append((pool_name, holder_label, Path(cwd)))
        if self.refuse is not None:
            raise self.refuse
        return StubGrant("lease-9", "research-9", "token-9")

    def end_lease(self, lease_id, kind, *, cause=None):
        self.endings.append((lease_id, kind, cause))


class StubClient:
    """An agent host's connection: answers each request, then plays *frames*."""

    def __init__(self, frames, *, state="idle", refuse_prompt=False):
        self.frames = list(frames)
        self.state = state
        self.refuse_prompt = refuse_prompt
        self.sent = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def request(self, obj, timeout=5.0):
        self.sent.append(obj)
        if obj["type"] == "status":
            return {"type": "response", "success": True,
                    "data": {"status": {"state": self.state}}}
        if obj["type"] == "prompt" and self.refuse_prompt:
            return {"type": "response", "success": False, "error": "busy"}
        return {"type": "response", "success": True}

    def read_until(self, predicate, timeout=None):
        for frame in self.frames:
            if predicate(frame):
                return frame
        raise TimeoutError("no frame")


def _adapter(monkeypatch, client, service=None):
    connected = []

    def connect(name, connect_timeout=5.0, lease_token=None):
        connected.append((name, lease_token))
        return client

    monkeypatch.setattr(run, "connect", connect)
    service = service or StubService()
    return run.ServicePool(service), service, connected


def test_the_adapter_leases_through_the_service_and_ends_through_it(monkeypatch, tmp_path):
    pool, service, _ = _adapter(monkeypatch, StubClient([]))

    async def main():
        leased = await pool.lease("research", cwd=tmp_path, label="gateway", env={})
        await pool.end(leased, "finished")
        return leased

    leased = asyncio.run(main())
    assert (leased.lease_id, leased.agent, leased.token) == ("lease-9", "research-9", "token-9")
    assert "token-9" not in repr(leased)
    assert service.grants == [("research", "gateway", tmp_path)]
    assert service.endings == [("lease-9", "released", "finished")]


def test_the_adapter_turns_a_refusal_into_a_run_fault(monkeypatch, tmp_path):
    service = StubService(refuse=SpecfloError("pool 'research' has no free member."))
    pool, _, _ = _adapter(monkeypatch, StubClient([]), service)

    with pytest.raises(run.RunError, match="no free member"):
        asyncio.run(pool.lease("research", cwd=tmp_path, label="gateway", env={}))


def _prompt(pool, timeout=5.0):
    leased = run.Leased(lease_id="lease-9", agent="research-9", token="token-9")
    return asyncio.run(pool.prompt(leased, "Research it.", timeout=timeout))


def test_the_adapter_sends_the_prompt_with_the_lease_token_and_waits_for_the_settle(monkeypatch):
    client = StubClient([
        {"type": "agent_start"},
        {"type": "turn_end", "message": {"stopReason": "stop"}},
        {"type": "agent_settled"},
    ])
    pool, _, connected = _adapter(monkeypatch, client)
    _prompt(pool)

    assert connected == [("research-9", "token-9")]
    assert [frame["type"] for frame in client.sent] == ["status", "prompt"]
    assert client.sent[1]["message"] == "Research it."
    assert client.closed


def test_the_adapter_fails_a_turn_that_ended_in_an_error(monkeypatch):
    client = StubClient([
        {"type": "turn_end", "message": {"stopReason": "error", "errorMessage": "quota"}},
        {"type": "agent_settled"},
    ])
    pool, _, _ = _adapter(monkeypatch, client)

    with pytest.raises(run.RunError, match="quota"):
        _prompt(pool)


def test_the_adapter_fails_when_pi_goes(monkeypatch):
    pool, _, _ = _adapter(monkeypatch, StubClient([{"type": "process_exit", "exit_code": 1}]))

    with pytest.raises(run.RunError, match="exited"):
        _prompt(pool)


def test_the_adapter_fails_at_its_own_limit(monkeypatch):
    pool, _, _ = _adapter(monkeypatch, StubClient([{"type": "agent_start"}]))

    with pytest.raises(run.RunError, match="did not finish in 2 seconds"):
        _prompt(pool, timeout=2)


@pytest.mark.parametrize("client", [
    StubClient([], state="exited"),
    StubClient([], refuse_prompt=True),
])
def test_the_adapter_fails_an_agent_that_does_not_take_the_prompt(monkeypatch, client):
    pool, _, _ = _adapter(monkeypatch, client)

    with pytest.raises(run.RunError):
        _prompt(pool)
