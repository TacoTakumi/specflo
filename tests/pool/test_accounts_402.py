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
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo.agent.cli import agent_app
from specflo.agent.statefiles import AgentPaths
from specflo.daemon import HEALTH_PATH
from specflo.daemon import app as daemon_app
from specflo.daemon.poolstore import open_pool_store
from specflo.pool import accounts, watch
from specflo.pool.config import Account

from .stub_provider import StubProvider
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
    assert wait_until(lambda: member.replies_logged() == 1), "the retried turn did not succeed"
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


# -- the daemon's lifecycle -------------------------------------------------


def test_the_watcher_is_for_a_pool_with_a_hosted_member(pool_rig):
    local = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    hosted = pool_rig.service(pool_rig.config(pool_rig.hosted_member()))

    assert watch.watcher_for(None) is None
    assert watch.watcher_for(local) is None
    watcher = watch.watcher_for(hosted)
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
