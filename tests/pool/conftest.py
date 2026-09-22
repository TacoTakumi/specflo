"""What the pool service's tests share: the daemon's surroundings with a pool in them.

The surroundings are the runner tests' own - a state directory, a fake herdr
and the stub pi behind a recorder - so a lease here starts and stops real
processes the same way. Added to them are a pool configuration held in
memory, the pool store on a daemon root, a clock that stands still until a
test moves it, and ids and tokens minted by counting.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from specflo.agent.statefiles import AgentPaths, read_status
from specflo.daemon.poolstore import PoolStore, open_pool_store
from specflo.pool import accounts
from specflo.pool.config import Member, Pool, PoolConfig
from specflo.pool.service import PoolService

from .test_runner import ACCOUNTS, DEFINITION, POOL_TOKEN, Rig

START = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """The time now, moved only by the test."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class PoolRig(Rig):
    """The runner tests' rig, with a pool store, a clock and a service on it."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        super().__init__(tmp_path, monkeypatch)
        self.root = tmp_path / "daemon"
        self.root.mkdir()
        self.clock = FakeClock()
        # A member writes inside its sandbox, where the working directory of
        # its lease is the one place it may.
        self.capture = self.work / "capture.jsonl"
        # minted by counting, across every service made on this rig
        self.ids = (f"lease-{n}" for n in range(1, 1000))
        self.tokens = (f"token-{n}" for n in range(1, 1000))

    def scenario(self, **keys) -> None:
        """What the stub pi of the members started from now on does."""
        (self.tmp_path / "scenario.json").write_text(json.dumps(keys), encoding="utf-8")

    def config(self, *members: Member, size: int | None = None, **pool) -> PoolConfig:
        """One pool, "rebasers", of *members*; by default as large as they are many."""
        fields = {"idle_default": 600, "idle_max": 4 * 3600, **pool}
        rebasers = Pool(
            name="rebasers", definition=DEFINITION.name,
            members=tuple(m.name for m in members),
            size=len(members) if size is None else size, **fields,
        )
        return PoolConfig(
            path=self.tmp_path / "pool" / "pool.yaml", accounts=ACCOUNTS,
            members=members, pools=(rebasers,), definitions=(DEFINITION,),
            models_file=self.models_file,
        )

    def service(self, config: PoolConfig) -> PoolService:
        return PoolService(
            config=config, open_store=self.store, pool_token=POOL_TOKEN,
            config_root=self.config_root, clock=self.clock,
            mint_id=lambda: next(self.ids), mint_token=lambda: next(self.tokens),
        )

    def store(self) -> PoolStore:
        return open_pool_store(self.root)

    def status(self, agent: str) -> dict:
        """The agent host's status record for *agent*."""
        return read_status(AgentPaths.resolve(agent).status)


@pytest.fixture(autouse=True)
def no_real_llama_swap(monkeypatch):
    # A test daemon that serves starts the events reader; point it at a closed
    # port so no test reads a real llama-swap, whatever runs on this machine.
    monkeypatch.setenv("SPECFLO_LLAMA_SWAP_URL", "http://127.0.0.1:1")


@pytest.fixture(autouse=True)
def no_real_provider(monkeypatch):
    # A test daemon that serves a pool with an account starts the accounts
    # reader; point every key read that is handed no client at a closed port,
    # so no test asks the real provider anything, whatever keys are set here.
    monkeypatch.setattr(accounts, "PROVIDER_URL", "http://127.0.0.1:1")


@pytest.fixture
def pool_rig(tmp_path, monkeypatch):
    rig = PoolRig(tmp_path, monkeypatch)
    yield rig
    rig.cleanup()
