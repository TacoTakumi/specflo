"""Model reloads of leased members, read from llama-swap's event stream.

A client outside the pool may ask llama-swap for a model that cannot run
beside a leased member's model; llama-swap then unloads the member's model
and loads it again on the member's next turn. The lease is untouched by
that. The daemon reads ``GET /api/events`` and stores each such reload, with
its time, against the member; what happens to a model under no lease stores
nothing. With the stream out of reach the reader waits longer and longer
between tries, the store says reload data is unavailable, and leases are
granted as ever. The reader sends llama-swap that one GET and nothing else.

The stream is a fixture text in the server's own framing, the clock is the
pool tests' fake one, and the transport is httpx's mock: nothing here opens
a connection.
"""

from __future__ import annotations

import ast
import asyncio
import shutil
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from specflo.daemon import HEALTH_PATH
from specflo.daemon import app as daemon_app
from specflo.daemon import pool_routes
from specflo.daemon.poolstore import Lease, Reload, Resource, Transition, open_pool_store
from specflo.pool import cli_admin, events
from specflo.pool import config as pool_config
from specflo.pool.config import Member, Pool, PoolConfig

from .conftest import FakeClock
from .test_cli_validate import _write_three_faults

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "pool"
STREAM = (FIXTURES / "llama-swap-events.txt").read_text(encoding="utf-8")
# Where the fixture's second part begins: the request that loads model-a again.
SECOND_LOAD = ": The next request for model-a"
SOURCE = Path(events.__file__)
URL = "http://llama-swap.test:8080"

T0 = "2026-03-01T12:00:00.000+00:00"
T90 = "2026-03-01T12:01:30.000+00:00"


def member(name: str, model: str, backing: str = "local") -> Member:
    return Member(
        name=name, command="pi --mode rpc", backing=backing, labels=(), capacity=1,
        egress="local" if backing == "local" else "no-train", model=model,
        account=None if backing == "local" else "team-a",
    )


def config_of(tmp_path: Path, *members: Member) -> PoolConfig:
    pool = Pool(
        name="rebasers", definition="rebaser", members=tuple(m.name for m in members),
        size=len(members), idle_default=600, idle_max=4 * 3600,
    )
    return PoolConfig(path=tmp_path / "pool" / "pool.yaml", members=members, pools=(pool,))


def lease_on(store, member_name: str, lease_id: str = "lease-1") -> None:
    """An active lease on *member_name*, as a grant writes it; no process is started."""
    store.add_lease(Lease(
        id=lease_id, team_lease_id=None, holder_hash="hash", holder_label="orchestrator-a",
        member=member_name, pool="rebasers",
        resources=(Resource("pool", "rebasers"), Resource("member", member_name)),
        acquired=T0, last_activity=T0, idle_limit=600, state="active",
    ))
    store.record_transition(
        Transition(id=0, lease_id=lease_id, kind="granted", time=T0, cause="requested"),
        expect="active",
    )


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "daemon"
    root.mkdir()
    return root


@pytest.fixture
def clock():
    return FakeClock()


def recorder_on(root, clock, *members: Member) -> events.Recorder:
    return events.Recorder(
        config_of(root, *members), lambda: open_pool_store(root), clock=clock
    )


def feed(recorder: events.Recorder, text: str) -> None:
    for line in text.splitlines():
        recorder.line(line)


# -- the stream's framing ---------------------------------------------------


def test_the_framer_gives_each_message_its_data_and_passes_over_comments():
    framer = events.Framer()

    messages = [data for line in STREAM.splitlines() if (data := framer.feed(line)) is not None]

    assert len(messages) == 27
    assert all(message.startswith('{"type":"') for message in messages)


def test_only_a_model_status_message_gives_model_states():
    framer = events.Framer()
    messages = [data for line in STREAM.splitlines() if (data := framer.feed(line)) is not None]

    states = [found for m in messages if (found := events.model_states(m)) is not None]

    assert len(states) == 15
    assert states[0] == {
        "model-a": "ready", "model-b": "stopped", "model-c": "stopped", "model-d": "stopped",
    }


@pytest.mark.parametrize("data", [
    "", "not json", "[]", '{"type":"modelStatus"}', '{"type":"modelStatus","data":"{"}',
    '{"type":"modelStatus","data":"{}"}', '{"type":"modelStatus","data":"[7]"}',
])
def test_a_message_that_is_not_a_whole_snapshot_gives_no_states(data):
    assert events.model_states(data) is None


# -- which leased member an event concerns ----------------------------------


def test_a_model_unloaded_and_loaded_again_under_one_lease_is_one_reload_of_its_member():
    tracker = events.ReloadTracker()
    leased = {"model-a": {"coder-a": {"lease-1"}}}

    assert tracker.observe({"model-a": "ready"}, leased) == []
    assert tracker.observe({"model-a": "stopping"}, leased) == []
    assert tracker.observe({"model-a": "stopped"}, leased) == []
    assert tracker.observe({"model-a": "starting"}, leased) == [("coder-a", "model-a")]
    assert tracker.observe({"model-a": "ready"}, leased) == []


def test_the_first_load_of_a_leased_model_is_not_a_reload():
    tracker = events.ReloadTracker()
    leased = {"model-a": {"coder-a": {"lease-1"}}}

    assert tracker.observe({"model-a": "stopped"}, leased) == []
    assert tracker.observe({"model-a": "starting"}, leased) == []
    assert tracker.observe({"model-a": "ready"}, leased) == []


def test_a_lease_granted_after_the_unload_did_not_see_a_reload():
    tracker = events.ReloadTracker()
    tracker.observe({"model-a": "ready"}, {"model-a": {"coder-a": {"lease-1"}}})
    tracker.observe({"model-a": "stopped"}, {"model-a": {"coder-a": {"lease-1"}}})

    # lease-1 has ended and lease-2 stands on the member when the model loads
    assert tracker.observe({"model-a": "starting"}, {"model-a": {"coder-a": {"lease-2"}}}) == []


def test_leased_models_are_the_models_of_local_members_under_an_active_lease(root):
    members = (member("coder-a", "model-a"), member("coder-b", "model-a"),
               member("idle-c", "model-c"), member("hosted-1", "vendor/model", "hosted"))
    with open_pool_store(root) as store:
        for number, name in enumerate(("coder-a", "coder-b", "hosted-1"), start=1):
            lease_on(store, name, f"lease-{number}")
        store.record_transition(
            Transition(id=0, lease_id="lease-2", kind="released", time=T0, cause="released"),
            expect="active",
        )
        leases = store.list_leases(state="active")

    assert events.leased_models(members, leases) == {"model-a": {"coder-a": {"lease-1"}}}


# -- the replay -------------------------------------------------------------


def test_the_replay_leaves_the_lease_active_and_stores_one_reload_with_its_time(root, clock):
    recorder = recorder_on(root, clock, member("coder-a", "model-a"))
    with open_pool_store(root) as store:
        lease_on(store, "coder-a")
    first, second = STREAM.split(SECOND_LOAD)

    feed(recorder, first)
    with open_pool_store(root) as store:
        # unloaded for the other client's model: nothing is stored until it loads again
        assert store.list_reloads() == []
        assert store.get_lease("lease-1").state == "active"

    clock.advance(seconds=90)
    feed(recorder, SECOND_LOAD + second)

    with open_pool_store(root) as store:
        assert store.list_reloads(member="coder-a") == [
            Reload(id=1, member="coder-a", model="model-a", time=T90)
        ]
        assert store.list_reloads() == store.list_reloads(member="coder-a")
        lease = store.get_lease("lease-1")
        assert (lease.state, lease.last_activity) == ("active", T0)
        assert [t.kind for t in store.list_transitions(lease_id="lease-1")] == ["granted"]


def test_events_for_models_under_no_lease_store_nothing(root, clock):
    # model-c is unloaded and loaded again in the stream, and so is model-a
    recorder = recorder_on(root, clock, member("coder-a", "model-a"), member("idle-c", "model-c"))

    feed(recorder, STREAM)

    with open_pool_store(root) as store:
        assert store.list_reloads() == []


def test_a_reload_after_the_lease_has_ended_stores_nothing(root, clock):
    recorder = recorder_on(root, clock, member("coder-a", "model-a"))
    with open_pool_store(root) as store:
        lease_on(store, "coder-a")
    first, second = STREAM.split(SECOND_LOAD)

    feed(recorder, first)
    with open_pool_store(root) as store:
        store.record_transition(
            Transition(id=0, lease_id="lease-1", kind="released", time=T0, cause="released"),
            expect="active",
        )
    feed(recorder, SECOND_LOAD + second)

    with open_pool_store(root) as store:
        assert store.list_reloads() == []


def test_a_connection_cut_in_the_middle_of_a_message_does_not_spoil_the_next_one(root, clock):
    recorder = recorder_on(root, clock, member("coder-a", "model-a"))
    with open_pool_store(root) as store:
        lease_on(store, "coder-a")

    recorder.line('data:{"type":"modelStatus","data":"[{\\"id\\":\\"mod')
    recorder.connected()
    feed(recorder, STREAM)

    with open_pool_store(root) as store:
        assert [r.member for r in store.list_reloads()] == ["coder-a"]
        assert store.reload_data_available() is True


# -- the reader -------------------------------------------------------------


class Stop(Exception):
    """Raised by a test's sleep to end the reader's loop."""


class Sleeps:
    """The reader's sleep: keeps each delay asked for, and ends the loop at the last one."""

    def __init__(self, last: int) -> None:
        self.last = last
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self.last:
            raise Stop


def run(reader: events.EventsReader) -> None:
    with pytest.raises(Stop):
        asyncio.run(reader.run())


def test_the_reader_stores_the_reload_and_sends_nothing_but_the_get_of_the_events_path(
    root, clock
):
    with open_pool_store(root) as store:
        lease_on(store, "coder-a")
    requests: list[httpx.Request] = []
    seen_available: list[bool] = []

    async def body():
        yield STREAM.encode("utf-8")
        with open_pool_store(root) as store:
            seen_available.append(store.reload_data_available())

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body())

    reader = events.EventsReader(
        recorder_on(root, clock, member("coder-a", "model-a")),
        url=URL, transport=httpx.MockTransport(answer), sleep=Sleeps(1),
    )
    run(reader)

    assert [(r.method, str(r.url), r.content) for r in requests] == [
        ("GET", f"{URL}/api/events", b""),
    ]
    assert seen_available == [True]
    with open_pool_store(root) as store:
        assert [(r.member, r.model, r.time) for r in store.list_reloads()] == [
            ("coder-a", "model-a", T0)
        ]
        # the reader has stopped: what is stored is no longer kept up
        assert store.reload_data_available() is False


def test_with_the_stream_unreachable_the_reader_backs_off_and_leases_still_grant(pool_rig):
    local = pool_rig.local_member()
    config = pool_rig.config(local)
    with pool_rig.store() as store:
        store.set_reload_data_available(True)
    requests: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    sleeps = Sleeps(9)
    reader = events.EventsReader(
        events.Recorder(config, pool_rig.store, clock=pool_rig.clock),
        url=URL, transport=httpx.MockTransport(refuse), sleep=sleeps,
    )
    run(reader)

    assert sleeps.delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert {(r.method, r.url.path) for r in requests} == {("GET", "/api/events")}
    with pool_rig.store() as store:
        assert store.reload_data_available() is False

    grant = pool_rig.service(config).grant(
        "rebasers", holder_label="orchestrator-a", cwd=pool_rig.work
    )
    with pool_rig.store() as store:
        assert store.get_lease(grant.lease_id).state == "active"
        assert store.reload_data_available() is False


def test_a_refused_answer_counts_as_unreachable_and_a_good_one_starts_the_wait_over(root, clock):
    answers = iter([503, 503, 200, 503])

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(answers), content=b"")

    sleeps = Sleeps(4)
    reader = events.EventsReader(
        recorder_on(root, clock, member("coder-a", "model-a")),
        url=URL, transport=httpx.MockTransport(answer), sleep=sleeps,
    )
    run(reader)

    assert sleeps.delays == [1, 2, 1, 2]


def test_the_reader_module_holds_no_request_but_the_get_of_the_events_path():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    calls = [
        ast.unparse(node.func) for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    # httpx is reached to make the one client and its timeout, and the client
    # to open the stream; no other name in the module is an httpx object
    assert sorted(c for c in calls if c.startswith("httpx.")) == [
        "httpx.AsyncClient", "httpx.Timeout",
    ]
    assert [c for c in calls if c.startswith("client.")] == ["client.stream"]
    streams = [
        ast.unparse(node) for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "client.stream"
    ]
    assert streams == ["client.stream('GET', EVENTS_PATH)"]
    assert events.EVENTS_PATH == "/api/events"
    paths = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value.startswith("/")
    }
    assert paths == {"/api/events"}


# -- where llama-swap is ----------------------------------------------------


def test_the_reader_is_for_a_pool_with_a_local_member_and_reads_where_the_environment_says(
    pool_rig,
):
    local = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    hosted = pool_rig.service(pool_rig.config(pool_rig.hosted_member()))

    # no pool, and a pool with no local member, have nothing to record for
    assert events.reader_for(None).recorder.recording() is False
    assert events.reader_for(hosted).recorder.recording() is False
    assert events.reader_for(local).recorder.recording() is True
    assert events.reader_for(local, environ={}).url == "http://127.0.0.1:8080"
    assert events.reader_for(local, environ={events.URL_ENV: URL}).url == URL


# -- a configuration put in force later -------------------------------------


def in_force(service, config) -> None:
    """Put *config* in force on *service*, as a reload that passed does."""
    assert service.swap(config, lambda *_: ()) == ()


def test_a_local_member_added_by_a_reload_is_recorded_from_the_next_snapshot(pool_rig):
    service = pool_rig.service(pool_rig.config(member("idle-c", "model-c")))
    recorder = events.reader_for(service).recorder
    with pool_rig.store() as store:
        lease_on(store, "coder-a")

    in_force(service, pool_rig.config(member("idle-c", "model-c"), member("coder-a", "model-a")))
    feed(recorder, STREAM)

    with pool_rig.store() as store:
        assert [(r.member, r.model) for r in store.list_reloads()] == [("coder-a", "model-a")]


class Pools:
    """What the daemon hands a reader in place of the pool it was made for:
    the pool in force now, none until a test puts one there."""

    def __init__(self, pool=None) -> None:
        self.pool = pool

    def __call__(self):
        return self.pool


@pytest.mark.parametrize("at_start", ["no pool", "no local member"])
def test_a_reader_with_nothing_to_record_for_connects_once_a_reload_brings_a_local_member(
    pool_rig, at_start
):
    hosted = pool_rig.config(pool_rig.hosted_member())
    local = pool_rig.config(pool_rig.hosted_member(), pool_rig.local_member())
    pools = Pools(None if at_start == "no pool" else pool_rig.service(hosted))
    with pool_rig.store() as store:
        store.set_reload_data_available(True)
    requests: list[httpx.Request] = []
    delays: list[float] = []
    connections: list[int] = []
    left_alone: list[bool] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"")

    async def sleep(delay: float) -> None:
        delays.append(delay)
        connections.append(len(requests))
        if len(delays) == 2:
            with pool_rig.store() as store:
                left_alone.append(store.reload_data_available())
            # the reload: the pool opens, or the pool that stands gets a local member
            if pools.pool is None:
                pools.pool = pool_rig.service(local)
            else:
                in_force(pools.pool, local)
        if len(delays) == 4:
            raise Stop

    reader = events.reader_for(pools(), environ={events.URL_ENV: URL})
    assert reader is not None, "a pool with nothing to record for has a reader that waits"
    reader.pool, reader.transport, reader.sleep = pools, httpx.MockTransport(answer), sleep
    run(reader)

    # two waits with nothing to record for, then the wait after each stream
    # that was answered and ended
    assert delays == [events.IDLE_INTERVAL, events.IDLE_INTERVAL, 1, 1]
    assert connections == [0, 0, 1, 2]
    # while it waited the reader wrote nothing
    assert left_alone == [True]


def test_a_reader_cancelled_while_it_waits_for_a_local_member_stops_and_writes_nothing(pool_rig):
    with pool_rig.store() as store:
        store.set_reload_data_available(True)
    waiting = asyncio.Event()

    async def sleep(delay: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    reader = events.reader_for(None, environ={events.URL_ENV: URL})
    assert reader is not None, "a daemon with no pool has a reader that waits"
    reader.sleep = sleep

    async def serve() -> None:
        task = asyncio.create_task(reader.run())
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(serve())

    with pool_rig.store() as store:
        assert store.reload_data_available() is True


# -- the daemon's lifecycle -------------------------------------------------


def write_pool(root: Path) -> None:
    """A pool directory that stands: one pool of one local member on model-a."""
    directory = cli_admin.pool_dir(root)
    folder = directory / pool_config.DEFINITIONS_DIR
    folder.mkdir(parents=True)
    (folder / "rebaser.md").write_text(
        "---\nrole: Rebases the work branch\ntools: [read, bash]\negress: local\n---\n\n"
        "You are the rebaser.\n",
        encoding="utf-8",
    )
    shutil.copy(FIXTURES / "llama-swap.yaml", directory / "llama-swap.yaml")
    shutil.copy(FIXTURES / "models.json", directory / "models.json")
    data = {
        "llama_swap": "llama-swap.yaml",
        "models_file": "models.json",
        "members": [{
            "name": "local-1", "command": "pi --mode rpc", "backing": "local",
            "model": "model-a", "labels": [], "capacity": 1, "egress": "local",
        }],
        "pools": [{
            "name": "rebasers", "definition": "rebaser", "members": ["local-1"],
            "size": 1, "idle_default": "10m", "idle_max": "4h",
        }],
    }
    (directory / pool_config.POOL_FILE).write_text(yaml.safe_dump(data), encoding="utf-8")


class Readers:
    """In place of ``reader_for``: the reader it makes reads a mock llama-swap
    that holds every connection open, and each pool asked about is kept."""

    def __init__(self, monkeypatch) -> None:
        self.asked: list[object] = []
        self.made: list[events.EventsReader] = []
        self.connected = 0
        self._make = events.reader_for
        monkeypatch.setattr(daemon_app.events, "reader_for", self)

    def __call__(self, pool):
        self.asked.append(pool)
        reader = self._make(pool, environ={events.URL_ENV: URL})
        if reader is not None:
            reader.transport = httpx.MockTransport(self._answer)
            self.made.append(reader)
        return reader

    def _answer(self, request: httpx.Request) -> httpx.Response:
        async def body():
            self.connected += 1
            yield b": connected\n\n"
            await asyncio.Event().wait()

        return httpx.Response(200, content=body())


def test_the_daemon_reads_the_stream_while_it_serves_a_pool_and_stops_at_shutdown(
    root, monkeypatch
):
    write_pool(root)
    readers = Readers(monkeypatch)
    application = daemon_app.create_app(root)
    # making the application reads nothing: the reader runs while the daemon serves
    assert readers.asked == []

    with TestClient(application) as client:
        assert client.get(HEALTH_PATH).status_code == 200
        assert readers.asked == [application.state.pool]
        deadline = 200
        while not readers.connected and (deadline := deadline - 1):
            client.get(HEALTH_PATH)
        with open_pool_store(root) as store:
            assert store.reload_data_available() is True

    assert readers.connected == 1
    with open_pool_store(root) as store:
        assert store.reload_data_available() is False


def test_a_daemon_with_no_pool_or_a_pool_with_faults_reads_no_stream(tmp_path, monkeypatch):
    readers = Readers(monkeypatch)
    bare = tmp_path / "bare"
    bare.mkdir()
    faulty = _write_three_faults(tmp_path / "faulty")

    for daemon_root in (bare, faulty):
        with TestClient(daemon_app.create_app(daemon_root)) as client:
            assert client.get(HEALTH_PATH).status_code == 200

    assert readers.asked == [None, None]
    assert readers.connected == 0


class Replays(Readers):
    """Readers whose mock llama-swap sends the fixture stream, then holds on."""

    def _answer(self, request: httpx.Request) -> httpx.Response:
        async def body():
            self.connected += 1
            yield STREAM.encode("utf-8")
            await asyncio.Event().wait()

        return httpx.Response(200, content=body())


def reloads_of(root: Path) -> list[tuple[str, str]]:
    with open_pool_store(root) as store:
        return [(r.member, r.model) for r in store.list_reloads()]


def test_a_daemon_that_got_its_pool_from_a_reload_records_model_reloads(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "IDLE_INTERVAL", 0.01)
    readers = Replays(monkeypatch)
    root = _write_three_faults(tmp_path / "daemon")
    application = daemon_app.create_app(root)
    assert application.state.pool is None
    with open_pool_store(root) as store:
        lease_on(store, "local-1")

    with TestClient(application) as client:
        assert client.get(HEALTH_PATH).status_code == 200
        # the admin mends the directory and has the daemon read it again
        shutil.rmtree(cli_admin.pool_dir(root))
        write_pool(root)
        assert pool_routes.reload_pool(application, "developer") == ()
        deadline = 500
        while not reloads_of(root) and (deadline := deadline - 1):
            client.get(HEALTH_PATH)

    assert readers.connected == 1
    assert reloads_of(root) == [("local-1", "model-a")]


def test_a_reload_that_is_refused_changes_no_reader(root, monkeypatch):
    monkeypatch.setattr(events, "IDLE_INTERVAL", 0.01)
    write_pool(root)
    readers = Readers(monkeypatch)
    application = daemon_app.create_app(root)

    with TestClient(application) as client:
        deadline = 200
        while not readers.connected and (deadline := deadline - 1):
            client.get(HEALTH_PATH)
        in_force_before = application.state.pool.config
        (cli_admin.pool_dir(root) / pool_config.POOL_FILE).write_text("members: 7\n")
        assert pool_routes.reload_pool(application, "developer") != ()
        for _ in range(20):
            client.get(HEALTH_PATH)
        assert readers.made[0].recorder.recording() is True
        assert readers.made[0].recorder.config is in_force_before

    # the one connection stood through the reload that did not pass
    assert readers.connected == 1
