"""A member's provider refusals, read from its event log by the daemon's watcher.

A hosted member's calls go to the provider under its account's key. When the
provider refuses one, pi ends the turn in an assistant message whose stop
reason is an error and whose error text is the status and the provider's error
object. The watcher reads the event logs of the hosted members under lease
and acts on a 402 by what it names as the limit's source: a key limit or
exhausted credits closes the account until the key's reset, and an in-flight
budget refusal leaves the account open and has the same prompt sent again
after the provider's Retry-After. A 429 is pi's own to retry and changes
nothing here.

The refusals come from the fake provider: a test asks it what the member's
call would be answered, and hands that answer to the stub pi, which reports
it the way pi reports a failed call. No test waits out a real delay: the
watcher's clock and its sleep are the test's.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo.agent.cli import agent_app
from specflo.agent.statefiles import AgentPaths
from specflo.daemon import HEALTH_PATH
from specflo.daemon import app as daemon_app
from specflo.daemon import pool_routes
from specflo.daemon.poolstore import open_pool_store
from specflo.pool import accounts, cli_admin, console, watch
from specflo.pool import config as pool_config
from specflo.pool.config import Account, Member

from .stub_provider import StubProvider
from .test_cli_validate import _write_three_faults
from .test_console_attach import AGENT, SLOT, start_host
from .test_events_reloads import write_pool
from .test_runner import POOL_TOKEN, wait_until

CHAT_PATH = "/api/v1/chat/completions"
KEY = "key-of-team-a"
ENVIRON = {"TEAM_A_KEY": KEY}
TEAM_A = Account(name="team-a", cap=2, key_env="TEAM_A_KEY")
REPLY = "the branch is rebased, says the member"
MESSAGE = "rebase the branch"

IN_FLIGHT = {
    "code": 402,
    "message": (
        "This request would exceed your available credits given your current "
        "in-flight requests. Retry after in-flight requests settle, or add credits."
    ),
    "metadata": {
        "reason": "in_flight_budget_exhausted",
        "limit_source": "openrouter_in_flight_budget",
        "remedy_hint": "Retry after your in-flight requests settle (see the Retry-After header).",
    },
}
RATE_LIMITED = {
    "code": 429,
    "message": "Rate limit exceeded",
    "metadata": {"error_type": "rate_limit_exceeded"},
}


def out_of(limit_source: str) -> dict:
    """The provider's error object for a 402 that names *limit_source*."""
    return {
        "code": 402,
        "message": "Insufficient credits or key limit reached.",
        "metadata": {"limit_source": limit_source, "remedy_hint": "Add credits."},
    }


class ChatProvider(StubProvider):
    """The fake provider with the members' own call added: each chat call is
    refused with the next refusal it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.refusals: list[httpx.Response] = []

    def refuse(self, error: dict, *, retry_after: int | None = None) -> None:
        headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
        self.refusals.append(httpx.Response(error["code"], json={"error": error}, headers=headers))

    def _answer(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == CHAT_PATH and self.refusals:
            return self.refusals.pop(0)
        return super()._answer(request)

    def as_pi_reports(self) -> list[dict]:
        """Every refusal waiting, as the stub pi's scenario takes one: what a
        member's call is answered, read off the answer itself."""
        entries = []
        with self.client() as client:
            while self.refusals:
                answer = client.post(CHAT_PATH, headers={"Authorization": f"Bearer {KEY}"}, json={})
                entry = {"status": answer.status_code, "error": answer.json()["error"]}
                if "retry-after" in answer.headers:
                    entry["retry_after"] = int(answer.headers["retry-after"])
                entries.append(entry)
        return entries


@pytest.fixture
def provider():
    stub = ChatProvider()
    stub.set_key(KEY, usage=5.0, limit=5.0, limit_remaining=0.0)
    return stub


def key_resets(provider: ChatProvider, period: str | None) -> None:
    provider.keys[KEY]["limit_reset"] = period


class Leased:
    """One hosted member under lease on the pool rig, with a watcher on it."""

    def __init__(self, pool_rig, provider: ChatProvider) -> None:
        self.rig = pool_rig
        pool_rig.scenario(
            reply=REPLY, capture=str(pool_rig.capture),
            provider_errors=provider.as_pi_reports(),
        )
        self.service = pool_rig.service(pool_rig.config(pool_rig.hosted_member()))
        self.client = provider.client()
        self.watcher = watch.Watcher(
            self.service.config, pool_rig.store, pool_token=POOL_TOKEN,
            clock=pool_rig.clock, client=self.client, environ=ENVIRON,
        )
        # the watcher is up before the lease, as in a daemon that serves
        self.watcher.poll()
        self.grant = self.service.grant(
            "rebasers", holder_label="orchestrator-a", cwd=pool_rig.work
        )

    def prompt(self, message: str = MESSAGE):
        """One turn on the member, as the lease's holder."""
        return CliRunner().invoke(
            agent_app, ["prompt", self.grant.agent, message, "--lease-token", self.grant.token]
        )

    def prompts_received(self) -> list[str]:
        """The message of every prompt the member's pi was sent."""
        if not self.rig.capture.exists():
            return []
        frames = [json.loads(line) for line in self.rig.capture.read_text().splitlines() if line]
        return [frame["message"] for frame in frames if frame["type"] == "prompt"]

    def replies_logged(self) -> int:
        """The turns in the member's event log that ended in the reply."""
        log = AgentPaths.resolve(self.grant.agent).events.read_text(encoding="utf-8")
        events = [json.loads(line) for line in log.splitlines() if line]
        return sum(
            1 for event in events
            if event["type"] == "message_end" and event["message"].get("stopReason") == "stop"
        )

    def account(self):
        with self.rig.store() as store:
            return store.get_account("team-a")

    def ledger(self) -> tuple:
        """What the pool has on record: the leases, how each moved, who waits
        and every account."""
        with self.rig.store() as store:
            return (
                store.list_leases(), store.list_transitions(),
                store.list_waiting(), store.list_accounts(),
            )

    def close(self) -> None:
        self.client.close()


@pytest.fixture
def leased(pool_rig, provider):
    made: list[Leased] = []

    def make() -> Leased:
        made.append(Leased(pool_rig, provider))
        return made[-1]

    yield make
    for one in made:
        one.close()


# -- what a provider error says ---------------------------------------------


def test_the_error_text_of_a_failed_call_gives_the_status_the_limit_source_and_the_wait():
    text = f"402: {json.dumps(IN_FLIGHT, separators=(',', ':'))}\nRetry-After: 3"

    assert watch.provider_error(text) == watch.ProviderError(
        status=402, limit_source="openrouter_in_flight_budget", retry_after=3.0
    )


def test_an_error_text_with_no_error_object_or_no_status_still_reads():
    assert watch.provider_error("429 Rate limit exceeded") == watch.ProviderError(
        status=429, limit_source=None, retry_after=None
    )
    assert watch.provider_error("402: {not json") == watch.ProviderError(402, None, None)
    assert watch.provider_error('402: {"metadata": "none"}') == watch.ProviderError(402, None, None)
    assert watch.provider_error("fetch failed") is None
    assert watch.provider_error("") is None


# -- the key's reset --------------------------------------------------------


def test_a_keys_limit_resets_at_the_next_utc_day_week_or_month():
    # a Wednesday
    now = datetime(2026, 3, 4, 15, 30, tzinfo=timezone.utc)

    assert accounts.next_key_reset("daily", now) == datetime(2026, 3, 5, tzinfo=timezone.utc)
    assert accounts.next_key_reset("weekly", now) == datetime(2026, 3, 9, tzinfo=timezone.utc)
    assert accounts.next_key_reset("monthly", now) == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert accounts.next_key_reset(None, now) is None
    assert accounts.next_key_reset("yearly", now) is None


def test_a_reset_is_never_the_moment_it_is_asked_at():
    monday = datetime(2026, 3, 9, tzinfo=timezone.utc)
    december = datetime(2026, 12, 1, tzinfo=timezone.utc)

    assert accounts.next_key_reset("weekly", monday) == monday + timedelta(days=7)
    assert accounts.next_key_reset("monthly", december) == datetime(2027, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("period", "reopen"),
    [
        ("weekly", "2026-03-02T00:00:00.000+00:00"),
        ("monthly", "2026-04-01T00:00:00.000+00:00"),
        # a limit that never resets: the account is tried again when the day turns
        (None, "2026-03-02T00:00:00.000+00:00"),
    ],
)
def test_closing_for_a_limit_reads_the_key_and_reopens_at_its_reset(
    pool_rig, provider, period, reopen
):
    key_resets(provider, period)

    with pool_rig.store() as store, provider.client() as client:
        record = accounts.close_for_limit(
            TEAM_A, store, clock=pool_rig.clock, client=client, environ=ENVIRON
        )

    assert (record.closed, record.reopen) == (True, reopen)
    assert (record.figures.limit, record.figures.remaining) == (5.0, 0.0)
    assert [(seen.method, seen.path) for seen in provider.seen] == [("GET", "/api/v1/key")]


def test_closing_for_a_limit_with_the_key_unread_reopens_when_the_day_turns(pool_rig, provider):
    provider.unreachable = True

    with pool_rig.store() as store, provider.client() as client:
        record = accounts.close_for_limit(
            TEAM_A, store, clock=pool_rig.clock, client=client, environ=ENVIRON
        )

    assert (record.closed, record.reopen) == (True, "2026-03-02T00:00:00.000+00:00")
    assert "unreachable" in record.figures.read_error


# -- the watcher on a leased member -----------------------------------------


@pytest.mark.parametrize("limit_source", ["openrouter_key_limit", "openrouter_credits"])
def test_a_402_for_the_key_limit_or_the_credits_closes_the_account_until_the_keys_reset(
    leased, provider, limit_source
):
    key_resets(provider, "monthly")
    provider.refuse(out_of(limit_source))
    member = leased()

    member.prompt()
    member.watcher.poll()

    account = member.account()
    assert (account.closed, account.reopen) == (True, "2026-04-01T00:00:00.000+00:00")
    # the turn is not sent again, and the lease is the holder's to end
    assert member.prompts_received() == [MESSAGE]
    with member.rig.store() as store:
        assert store.get_lease(member.grant.lease_id).state == "active"
    # acted on once: the next pass finds nothing new in the log
    member.watcher.poll()
    assert [seen.path for seen in provider.seen].count("/api/v1/key") == 1


class Sleeps:
    """The watcher's sleep: moves the clock by each delay, notes what *seen*
    gives at that moment, and ends the loop at the last one."""

    class Stop(Exception):
        pass

    def __init__(self, clock, last: int, seen) -> None:
        self.clock, self.last, self.seen = clock, last, seen
        self.delays: list[float] = []
        self.saw: list = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.saw.append(self.seen())
        if len(self.delays) >= self.last:
            raise self.Stop
        self.clock.advance(seconds=delay)


def test_an_in_flight_402_leaves_the_account_open_and_the_turn_is_sent_again_after_the_wait(
    leased, provider
):
    provider.refuse(IN_FLIGHT, retry_after=3)
    member = leased()
    member.prompt()
    assert member.replies_logged() == 0
    sleeps = Sleeps(member.rig.clock, 3, lambda: len(member.prompts_received()))
    member.watcher.sleep = sleeps

    with pytest.raises(Sleeps.Stop):
        asyncio.run(member.watcher.run())

    # a pass every two seconds, and one at the moment the wait is over: the
    # prompt goes again at three seconds and not before
    assert sleeps.delays == [2.0, 1.0, 2.0]
    assert sleeps.saw == [1, 1, 2]
    assert member.prompts_received() == [MESSAGE, MESSAGE]
    wait_until(lambda: member.replies_logged() == 1, message="the retried turn did not succeed")
    last = CliRunner().invoke(
        agent_app, ["last", member.grant.agent, "--lease-token", member.grant.token]
    )
    assert last.stdout.strip() == REPLY
    assert member.account() is None  # nothing was written about it: it is open
    assert accounts.is_open(member.account(), member.rig.clock())
    # the turn that succeeded is not sent a third time
    member.watcher.poll()
    member.rig.clock.advance(seconds=10)
    member.watcher.poll()
    assert member.prompts_received() == [MESSAGE, MESSAGE]


def test_an_in_flight_402_that_names_no_wait_is_sent_again_after_the_default(leased, provider):
    provider.refuse(IN_FLIGHT)
    member = leased()
    member.prompt()

    assert member.watcher.poll() == watch.DEFAULT_RETRY_AFTER
    member.rig.clock.advance(seconds=watch.DEFAULT_RETRY_AFTER - 0.5)
    member.watcher.poll()
    assert member.prompts_received() == [MESSAGE]
    member.rig.clock.advance(seconds=0.5)
    member.watcher.poll()

    assert member.prompts_received() == [MESSAGE, MESSAGE]


def test_a_429_leaves_the_account_open_and_the_ledger_as_it_was(leased, provider):
    provider.refuse(RATE_LIMITED, retry_after=7)
    member = leased()
    before = member.ledger()

    answered = member.prompt()
    member.watcher.poll()
    member.rig.clock.advance(seconds=60)
    member.watcher.poll()

    # pi retried its own call: the member had a retryable error and an answer
    assert answered.stdout.strip() == REPLY
    assert member.ledger() == before
    assert accounts.is_open(member.account(), member.rig.clock())
    assert member.prompts_received() == [MESSAGE]
    assert [seen.path for seen in provider.seen if seen.method == "GET"] == []


def test_the_watcher_and_the_store_hold_no_assistant_text_after_a_turn(leased, provider):
    provider.refuse(IN_FLIGHT, retry_after=3)
    member = leased()
    member.prompt()
    member.watcher.poll()
    member.rig.clock.advance(seconds=3)
    member.watcher.poll()
    assert wait_until(lambda: member.replies_logged() == 1)
    assert member.prompt("and again").stdout.strip() == REPLY
    member.watcher.poll()

    def held(value) -> str:
        """Everything the watcher keeps, down to its records' fields."""
        if isinstance(value, dict):
            return " ".join(held(k) + " " + held(v) for k, v in value.items())
        if isinstance(value, (list, tuple, set)):
            return " ".join(held(v) for v in value)
        if hasattr(value, "__dict__") and type(value).__module__ == watch.__name__:
            return held(vars(value))
        return repr(value)

    kept = held(member.watcher)
    assert "offset" in kept  # the walk did reach the watcher's own records
    assert REPLY not in kept and "rebased" not in kept
    for path in member.rig.root.rglob("*"):
        if path.is_file():
            assert b"rebased" not in path.read_bytes(), path


def test_a_lease_that_stood_before_the_watcher_is_read_from_now_on(pool_rig, provider):
    # a daemon that starts again finds leases whose logs it has not followed:
    # what they hold already is not acted on a second time
    provider.refuse(out_of("openrouter_key_limit"))
    pool_rig.scenario(reply=REPLY, provider_errors=provider.as_pi_reports())
    service = pool_rig.service(pool_rig.config(pool_rig.hosted_member()))
    grant = service.grant("rebasers", holder_label="orchestrator-a", cwd=pool_rig.work)
    CliRunner().invoke(agent_app, ["prompt", grant.agent, MESSAGE, "--lease-token", grant.token])

    with provider.client() as client:
        late = watch.Watcher(
            service.config, pool_rig.store, pool_token=POOL_TOKEN,
            clock=pool_rig.clock, client=client, environ=ENVIRON,
        )
        late.poll()
        late.poll()

    with pool_rig.store() as store:
        assert store.get_account("team-a") is None


def test_a_members_next_lease_is_not_read_the_errors_of_its_last(leased, provider):
    key_resets(provider, "daily")
    provider.refuse(out_of("openrouter_key_limit"))
    member = leased()
    member.prompt()
    member.watcher.poll()
    member.service.end_lease(member.grant.lease_id, "released")
    member.rig.clock.advance(days=2)
    with member.rig.store() as store:
        store.set_account_open("team-a")

    member.grant = member.service.grant(
        "rebasers", holder_label="orchestrator-a", cwd=member.rig.work
    )
    member.watcher.poll()

    assert member.account().closed is False


def test_a_local_member_is_not_watched(pool_rig, provider, monkeypatch):
    service = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    service.grant("rebasers", holder_label="orchestrator-a", cwd=pool_rig.work)
    read: list[str] = []
    monkeypatch.setattr(watch.runner, "read_log", lambda name, *a, **k: read.append(name))

    with provider.client() as client:
        watcher = watch.Watcher(
            service.config, pool_rig.store, pool_token=POOL_TOKEN,
            clock=pool_rig.clock, client=client, environ=ENVIRON,
        )
        watcher.poll()
        watcher.poll()

    assert read == []


# -- a lease whose agent is not named like its member -------------------------


def prompts_in(capture: Path) -> list[str]:
    """The message of every prompt the pi that writes *capture* was sent."""
    if not capture.exists():
        return []
    frames = [json.loads(line) for line in capture.read_text().splitlines() if line]
    return [frame["message"] for frame in frames if frame["type"] == "prompt"]


def turn(agent: str, message: str = MESSAGE, *, token: str | None = None):
    """One turn on *agent*: the holder's under the lease's *token*, the
    developer's own without one."""
    wall = [] if token is None else ["--lease-token", token]
    return CliRunner().invoke(agent_app, ["prompt", agent, message, *wall])


def answers(agent: str, token: str) -> bool:
    """Does the pi of *agent* answer the holder's question for its last text?
    The stub pi reads its scenario before it reads a question."""
    return CliRunner().invoke(agent_app, ["last", agent, "--lease-token", token]).exit_code == 0


@pytest.fixture
def resent(monkeypatch):
    """The agent of every prompt the watcher has sent again; each one is still sent."""
    agents: list[str] = []
    real = watch.runner.resend_prompt

    def resend(name, *args, **kwargs):
        agents.append(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(watch.runner, "resend_prompt", resend)
    return agents


class Shared:
    """One hosted member that serves two leases at a time, with a watcher on it.

    Each lease runs on an agent of its own name, the second one not the
    member's. The pi of each is started with the refusals the provider holds
    at that moment, and writes what it is sent to a capture of its own."""

    def __init__(self, pool_rig, provider: ChatProvider) -> None:
        self.rig, self.provider = pool_rig, provider
        member = replace(pool_rig.hosted_member(), capacity=2)
        self.service = pool_rig.service(pool_rig.config(member, size=2))
        self.client = provider.client()
        self.watcher = watch.Watcher(
            self.service.config, pool_rig.store, pool_token=POOL_TOKEN,
            clock=pool_rig.clock, client=self.client, environ=ENVIRON,
        )
        self.watcher.poll()
        self.captures: dict[str, Path] = {}

    def lease(self):
        """One more lease on the member; its pi is refused what the provider holds now."""
        capture = self.rig.work / f"capture-{len(self.captures) + 1}.jsonl"
        self.rig.scenario(
            reply=REPLY, capture=str(capture), provider_errors=self.provider.as_pi_reports()
        )
        grant = self.service.grant("rebasers", holder_label="orchestrator-a", cwd=self.rig.work)
        self.captures[grant.agent] = capture
        # The grant is back before its pi has read the scenario, and the next
        # lease writes the scenario again: a pi that answers has read it.
        wait_until(
            lambda: answers(grant.agent, grant.token),
            message=f"the pi of {grant.agent} did not answer",
        )
        return grant

    def prompts_received(self, grant) -> list[str]:
        return prompts_in(self.captures[grant.agent])

    def account(self):
        with self.rig.store() as store:
            return store.get_account("team-a")


@pytest.fixture
def shared(pool_rig, provider):
    member = Shared(pool_rig, provider)
    yield member
    member.client.close()


def test_a_402_for_the_key_limit_at_a_members_second_agent_closes_the_account(shared, provider):
    key_resets(provider, "monthly")
    first = shared.lease()
    provider.refuse(out_of("openrouter_key_limit"))
    second = shared.lease()
    assert (first.agent, second.agent) == ("hosted-1", "hosted-1.2")

    turn(second.agent, token=second.token)
    shared.watcher.poll()

    account = shared.account()
    assert account is not None, "the log of the second agent was not read"
    assert (account.closed, account.reopen) == (True, "2026-04-01T00:00:00.000+00:00")
    # acted on once, though two leases of the member are watched
    shared.watcher.poll()
    assert [seen.path for seen in provider.seen].count("/api/v1/key") == 1


@pytest.mark.parametrize("refused", [
    pytest.param(0, id="the first agent"),
    pytest.param(1, id="the second agent"),
])
def test_an_in_flight_402_at_one_of_a_members_agents_is_sent_again_once_and_to_that_agent(
    shared, provider, resent, refused
):
    grants = []
    for position in (0, 1):
        if position == refused:
            provider.refuse(IN_FLIGHT, retry_after=3)
        grants.append(shared.lease())
    mine, other = grants[refused], grants[1 - refused]

    turn(mine.agent, token=mine.token)
    assert shared.watcher.poll() == 3.0
    shared.rig.clock.advance(seconds=3)
    shared.watcher.poll()
    shared.rig.clock.advance(seconds=10)
    shared.watcher.poll()

    assert resent == [mine.agent]
    assert shared.prompts_received(mine) == [MESSAGE, MESSAGE]
    assert shared.prompts_received(other) == []
    assert shared.account() is None  # nothing was written about it: it is open


def test_a_members_second_lease_is_not_read_the_earlier_failures_of_its_first_agent(
    shared, provider, resent
):
    key_resets(provider, "daily")
    provider.refuse(out_of("openrouter_key_limit"))
    provider.refuse(IN_FLIGHT, retry_after=3)
    first = shared.lease()
    turn(first.agent, token=first.token)
    turn(first.agent, "and push it", token=first.token)
    shared.watcher.poll()
    assert shared.account().closed is True
    shared.rig.clock.advance(seconds=3)
    shared.watcher.poll()
    assert resent == [first.agent]
    # the account is open again while the first lease is still out
    with shared.rig.store() as store:
        store.set_account_open("team-a")

    second = shared.lease()
    assert second.agent != first.agent
    shared.watcher.poll()
    shared.rig.clock.advance(seconds=10)
    shared.watcher.poll()

    assert shared.account().closed is False
    assert resent == [first.agent]
    assert shared.prompts_received(first) == [MESSAGE, "and push it", "and push it"]
    assert shared.prompts_received(second) == []


def hosted_console() -> Member:
    """A console slot on a hosted model, its calls under the account's key."""
    return Member(
        name=SLOT, command="", backing="hosted", labels=(), capacity=1, egress="no-train",
        model="some-vendor/some-model", account="team-a", kind="console",
    )


class Desk:
    """A developer's own agent, attached to a hosted console slot, with a
    watcher on the pool. The agent's name is the developer's, not the slot's,
    and its pi is refused what the provider held when it was started."""

    def __init__(self, pool_rig, provider: ChatProvider) -> None:
        self.rig = pool_rig
        pool_rig.scenario(
            reply=REPLY, capture=str(pool_rig.capture),
            provider_errors=provider.as_pi_reports(),
        )
        start_host(pool_rig)
        self.service = pool_rig.service(pool_rig.config(hosted_console()))
        console.attach(self.service, SLOT, AGENT)
        self.client = provider.client()
        self.watcher = watch.Watcher(
            self.service.config, pool_rig.store, pool_token=POOL_TOKEN,
            clock=pool_rig.clock, client=self.client, environ=ENVIRON,
        )
        self.watcher.poll()

    def lease(self):
        grant = self.service.grant("rebasers", holder_label="orchestrator-a", cwd=self.rig.work)
        assert grant.agent == AGENT != SLOT
        return grant

    def prompts_received(self) -> list[str]:
        return prompts_in(self.rig.capture)

    def account(self):
        with self.rig.store() as store:
            return store.get_account("team-a")


@pytest.fixture
def desk(pool_rig, provider):
    made: list[Desk] = []

    def make() -> Desk:
        made.append(Desk(pool_rig, provider))
        return made[-1]

    yield make
    for one in made:
        one.client.close()


def test_a_402_for_the_key_limit_at_a_consoles_agent_closes_the_account(desk, provider):
    key_resets(provider, "monthly")
    provider.refuse(out_of("openrouter_key_limit"))
    slot = desk()
    grant = slot.lease()

    turn(AGENT, token=grant.token)
    slot.watcher.poll()

    account = slot.account()
    assert account is not None, "the log of the attached agent was not read"
    assert (account.closed, account.reopen) == (True, "2026-04-01T00:00:00.000+00:00")
    slot.watcher.poll()
    assert [seen.path for seen in provider.seen].count("/api/v1/key") == 1


def test_an_in_flight_402_at_a_consoles_agent_is_sent_again_once_and_to_that_agent(
    desk, provider, resent
):
    provider.refuse(IN_FLIGHT, retry_after=3)
    slot = desk()
    grant = slot.lease()

    turn(AGENT, token=grant.token)
    assert slot.watcher.poll() == 3.0
    slot.rig.clock.advance(seconds=3)
    slot.watcher.poll()
    slot.rig.clock.advance(seconds=10)
    slot.watcher.poll()

    assert resent == [AGENT]
    assert slot.prompts_received() == [MESSAGE, MESSAGE]
    assert slot.account() is None


def test_a_lease_on_a_console_is_not_read_what_its_agent_failed_at_before_the_lease(
    desk, provider, resent
):
    # the developer's host runs on from before the lease: its log holds the
    # developer's own turns, and their refusals are not the lease's
    provider.refuse(out_of("openrouter_key_limit"))
    provider.refuse(IN_FLIGHT, retry_after=3)
    slot = desk()
    turn(AGENT, "my own turn")
    turn(AGENT, "my next turn")
    assert slot.prompts_received() == ["my own turn", "my next turn"]
    # a pass goes by with no lease on the slot, as in a daemon that serves
    slot.watcher.poll()

    grant = slot.lease()
    slot.watcher.poll()
    slot.rig.clock.advance(seconds=10)
    slot.watcher.poll()

    assert slot.account() is None
    assert resent == []
    assert slot.prompts_received() == ["my own turn", "my next turn"]
    # what the agent is refused from now on is the lease's
    assert turn(AGENT, token=grant.token).stdout.strip() == REPLY


# -- a configuration put in force later ---------------------------------------


class Pools:
    """What the daemon hands a watcher in place of the pool it was made for:
    the pool in force now, none until a test puts one there."""

    def __init__(self, pool=None) -> None:
        self.pool = pool

    def __call__(self):
        return self.pool


def in_force(service, config) -> None:
    """Put *config* in force on *service*, as a reload that passed does."""
    assert service.swap(config, lambda *_: ()) == ()


@pytest.mark.parametrize("at_start", ["no pool", "no hosted member"])
def test_a_hosted_member_and_its_account_brought_by_a_reload_are_watched_from_the_next_pass(
    pool_rig, provider, monkeypatch, at_start
):
    key_resets(provider, "monthly")
    provider.refuse(out_of("openrouter_key_limit"))
    pool_rig.scenario(reply=REPLY, provider_errors=provider.as_pi_reports())
    before = replace(pool_rig.config(pool_rig.local_member()), accounts=())
    after = pool_rig.config(pool_rig.hosted_member())
    pools = Pools(None if at_start == "no pool" else pool_rig.service(before))
    read: list[str] = []
    read_log = watch.runner.read_log
    monkeypatch.setattr(
        watch.runner, "read_log", lambda name, *a, **k: read.append(name) or read_log(name, *a, **k)
    )

    with provider.client() as client:
        watcher = watch.watcher_for(pools())
        assert watcher is not None, "a pool with nothing to watch has a watcher that waits"
        watcher.pool, watcher.client = pools, client
        watcher.poll()
        with pool_rig.store() as store:
            assert read == [] and store.list_accounts() == []
        # the reload: the pool opens, or the pool that stands gets a hosted member
        if pools.pool is None:
            pools.pool = pool_rig.service(after)
        else:
            in_force(pools.pool, after)
        watcher.poll()
        grant = pools.pool.grant("rebasers", holder_label="orchestrator-a", cwd=pool_rig.work)
        CliRunner().invoke(
            agent_app, ["prompt", grant.agent, MESSAGE, "--lease-token", grant.token]
        )
        watcher.poll()

    assert read == ["hosted-1"]
    with pool_rig.store() as store:
        account = store.get_account("team-a")
    assert (account.closed, account.reopen) == (True, "2026-04-01T00:00:00.000+00:00")


def test_a_refusal_under_an_account_the_configuration_does_not_declare_is_said_and_passed_over(
    leased, provider, caplog
):
    provider.refuse(out_of("openrouter_key_limit"))
    member = leased()
    member.watcher.config = replace(member.service.config, accounts=())
    member.prompt()

    with caplog.at_level("WARNING", logger=watch.__name__):
        member.watcher.poll()

    assert member.account() is None
    assert [seen.path for seen in provider.seen] == []
    said = [r.getMessage() for r in caplog.records]
    assert said == [
        "account team-a is not declared: the provider's refusal of agent hosted-1 for "
        "openrouter_key_limit closes nothing"
    ]
    # acted on once, and the watching goes on
    member.watcher.poll()
    assert len(caplog.records) == 1


def test_the_loop_makes_no_pass_until_a_reload_brings_a_hosted_member(pool_rig, monkeypatch):
    pools = Pools()
    watcher = watch.watcher_for(None)
    assert watcher is not None, "a daemon with no pool has a watcher that waits"
    watcher.pool = pools
    passes: list[int] = []
    hosted = pool_rig.service(pool_rig.config(pool_rig.hosted_member()))

    def seen() -> int:
        if len(sleeps.delays) == 2:
            pools.pool = hosted
        return len(passes)

    monkeypatch.setattr(watcher, "poll", lambda: passes.append(len(passes)))
    sleeps = Sleeps(pool_rig.clock, 4, seen)
    watcher.sleep = sleeps

    with pytest.raises(Sleeps.Stop):
        asyncio.run(watcher.run())

    assert sleeps.delays == [watch.POLL_INTERVAL] * 4
    # no pass while there was nothing to watch, and one at each turn from then on
    assert sleeps.saw == [0, 0, 1, 2]


def test_a_watcher_cancelled_while_it_has_nothing_to_watch_stops(pool_rig):
    waiting = asyncio.Event()

    async def sleep(delay: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    watcher = watch.watcher_for(None)
    assert watcher is not None, "a daemon with no pool has a watcher that waits"
    watcher.sleep = sleep

    async def serve() -> None:
        task = asyncio.create_task(watcher.run())
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(serve())

    with pool_rig.store() as store:
        assert store.list_accounts() == []


def test_a_watcher_cancelled_in_the_middle_of_a_pass_stops(pool_rig, monkeypatch):
    watcher = watch.watcher_for(pool_rig.service(pool_rig.config(pool_rig.hosted_member())))
    begun, let_go = threading.Event(), threading.Event()

    def poll() -> None:
        begun.set()
        assert let_go.wait(5), "the pass was never let go"

    monkeypatch.setattr(watcher, "poll", poll)

    async def serve() -> None:
        task = asyncio.create_task(watcher.run())
        deadline = 500
        while not begun.is_set() and (deadline := deadline - 1):
            await asyncio.sleep(0.01)
        assert begun.is_set(), "the watcher made no pass"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        let_go.set()

    asyncio.run(serve())


# -- the daemon's lifecycle -------------------------------------------------


def test_the_watcher_is_for_a_pool_with_a_hosted_member(pool_rig):
    local = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    hosted = pool_rig.service(pool_rig.config(pool_rig.hosted_member()))

    # no pool, and a pool with no hosted member, have nothing to watch
    assert watch.watcher_for(None).watching() is False
    assert watch.watcher_for(local).watching() is False
    watcher = watch.watcher_for(hosted)
    assert watcher.watching() is True
    assert (watcher.config, watcher.pool_token, watcher.clock) == (
        hosted.config, POOL_TOKEN, pool_rig.clock
    )


def test_one_pass_that_fails_does_not_end_the_watching(pool_rig, monkeypatch):
    service = pool_rig.service(pool_rig.config(pool_rig.hosted_member()))
    watcher = watch.watcher_for(service)
    passes: list[int] = []

    def poll():
        passes.append(len(passes))
        raise OSError("the store is locked")

    monkeypatch.setattr(watcher, "poll", poll)
    sleeps = Sleeps(pool_rig.clock, 2, lambda: None)
    watcher.sleep = sleeps

    with pytest.raises(Sleeps.Stop):
        asyncio.run(watcher.run())

    assert passes == [0, 1]
    assert sleeps.delays == [watch.POLL_INTERVAL, watch.POLL_INTERVAL]


class Watchers:
    """In place of ``watcher_for``: each pool asked about is kept, and the
    watcher handed back only says whether it runs."""

    def __init__(self, monkeypatch, *, watching: bool) -> None:
        self.asked: list[object] = []
        self.watching = watching
        self.running = False
        self.started = 0
        monkeypatch.setattr(daemon_app.watch, "watcher_for", self)

    def __call__(self, pool):
        self.asked.append(pool)
        return self if self.watching else None

    async def run(self) -> None:
        self.started += 1
        self.running = True
        try:
            await asyncio.Event().wait()
        finally:
            self.running = False


def test_the_daemon_watches_while_it_serves_and_stops_at_shutdown(tmp_path, monkeypatch):
    root = tmp_path / "daemon"
    root.mkdir()
    write_pool(root)
    watchers = Watchers(monkeypatch, watching=True)
    application = daemon_app.create_app(root)
    # making the application watches nothing: the watcher runs while the daemon serves
    assert watchers.asked == []

    with TestClient(application) as client:
        assert client.get(HEALTH_PATH).status_code == 200
        assert watchers.asked == [application.state.pool]
        deadline = 200
        while not watchers.running and (deadline := deadline - 1):
            client.get(HEALTH_PATH)
        assert watchers.running

    assert (watchers.started, watchers.running) == (1, False)


def test_a_daemon_with_nothing_to_watch_starts_no_watcher(tmp_path, monkeypatch):
    watchers = Watchers(monkeypatch, watching=False)
    bare = tmp_path / "bare"
    bare.mkdir()

    with TestClient(daemon_app.create_app(bare)) as client:
        assert client.get(HEALTH_PATH).status_code == 200

    assert watchers.asked == [None]
    assert watchers.started == 0
    with open_pool_store(bare) as store:
        assert store.list_accounts() == []


def write_hosted_pool(rig) -> None:
    """A pool directory that stands: one pool of one hosted member, the rig's
    pi double, under the account "team-a"."""
    write_pool(rig.root)
    directory = cli_admin.pool_dir(rig.root)
    definition = directory / pool_config.DEFINITIONS_DIR / "rebaser.md"
    definition.write_text(
        definition.read_text(encoding="utf-8").replace("egress: local", "egress: no-train"),
        encoding="utf-8",
    )
    data = {
        "llama_swap": "llama-swap.yaml",
        "accounts": [{"name": "team-a", "cap": 2, "key_env": "TEAM_A_KEY"}],
        "members": [{
            "name": "hosted-1", "command": f"{rig.command} --model vendor/strong", "backing": "hosted",
            "model": "vendor/strong", "account": "team-a", "labels": [],
            "capacity": 1, "egress": "no-train",
        }],
        "pools": [{
            "name": "rebasers", "definition": "rebaser", "members": ["hosted-1"],
            "size": 1, "idle_default": "10m", "idle_max": "4h",
        }],
    }
    (directory / pool_config.POOL_FILE).write_text(yaml.safe_dump(data), encoding="utf-8")


def test_a_daemon_that_got_its_pool_from_a_reload_acts_on_a_members_402(
    pool_rig, provider, monkeypatch
):
    monkeypatch.setattr(watch, "POLL_INTERVAL", 0.01)
    provider.refuse(out_of("openrouter_key_limit"))
    pool_rig.scenario(reply=REPLY, provider_errors=provider.as_pi_reports())
    _write_three_faults(pool_rig.root)
    application = daemon_app.create_app(pool_rig.root)
    assert application.state.pool is None

    def closed() -> bool:
        with pool_rig.store() as store:
            account = store.get_account("team-a")
        return account is not None and account.closed

    with TestClient(application) as client:
        assert client.get(HEALTH_PATH).status_code == 200
        # the admin mends the directory and has the daemon read it again
        shutil.rmtree(cli_admin.pool_dir(pool_rig.root))
        write_hosted_pool(pool_rig)
        assert pool_routes.reload_pool(application, "developer") == ()
        service = application.state.pool
        grant = service.grant("rebasers", holder_label="orchestrator-a", cwd=pool_rig.work)
        CliRunner().invoke(
            agent_app, ["prompt", grant.agent, MESSAGE, "--lease-token", grant.token]
        )
        deadline = 500
        while not closed() and (deadline := deadline - 1):
            client.get(HEALTH_PATH)
        service.end_lease(grant.lease_id, "released")

    assert closed(), "the refusal of the member's call closed no account"
