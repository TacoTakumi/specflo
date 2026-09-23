"""Which models the bridge lets a completion name: the ones the ledger holds.

A member reaches llama-swap through the bridge, and any process in its
sandbox can name any model in a request. A model nothing holds is one the
ledger did not admit: loading it could evict a model another lease holds,
and the ledger would not know the rig holds it. So the daemon hands the
bridge the models held now - by an active lease or by a project agent - under
every name llama-swap answers them by.

Nothing here reaches the rig: the store is a stub holding lease rows.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import httpx
from starlette.testclient import TestClient

from specflo.daemon import app as daemon_app
from specflo.daemon.poolstore import Resource
from specflo.pool import bridge, events, ledger, matrix
from specflo.pool.cli_admin import pool_dir
from specflo.pool.config import PoolConfig
from specflo.pool.service import PoolService, held_models

SWAP = matrix.SwapConfig(
    model_ids=("coder-q3", "helper-q4", "other-q8"),
    vars={},
    combinations=(),
    aliases={"Coder-Q3": "coder-q3", "coder-small": "coder-q3", "Helper-Q4": "helper-q4"},
)


class Store:
    """The pool store as far as the held models are read from it."""

    def __init__(self, leases: list) -> None:
        self.leases = leases
        self.asked: list[str | None] = []

    def list_leases(self, *, state: str | None = None, **_) -> list:
        self.asked.append(state)
        return list(self.leases) if state == ledger.ACTIVE else []


def lease(*resources: tuple[str, str]) -> SimpleNamespace:
    return SimpleNamespace(resources=tuple(Resource(kind, name) for kind, name in resources))


def service(
    leases: list, standing: tuple = (), swap: matrix.SwapConfig | None = SWAP
) -> PoolService:
    return unexpiring(PoolService(
        config=PoolConfig(path=Path("pool.yaml"), swap=swap),
        open_store=lambda: contextlib.nullcontext(Store(leases)),
        pool_token="token",
        config_root=Path("piconfig"),
        standing=lambda: standing,
    ))


def unexpiring(pool: PoolService) -> PoolService:
    """*pool* with its expiry pass stubbed out. The pass comes first, as at
    every entry point, and the stub store holds too little of a lease row for
    the real one; the structural test of entry points holds it in place."""
    pool.expire_due = lambda own=None: []
    return pool


def test_a_leased_model_is_held_under_its_id_and_every_alias() -> None:
    held = service([lease((ledger.POOL, "workers"), (ledger.MODEL, "coder-q3"))]).held_models()

    assert held == {"coder-q3", "Coder-Q3", "coder-small"}


def test_every_active_lease_and_project_agent_holds_its_model() -> None:
    agent = ledger.Standing(
        id="standing-1", project="p", agent="p-agent",
        resources=(Resource(ledger.MODEL, "helper-q4"),),
    )
    held = service(
        [lease((ledger.MODEL, "coder-q3")), lease((ledger.ACCOUNT, "openrouter-main"))],
        standing=(agent,),
    ).held_models()

    assert held == {"coder-q3", "Coder-Q3", "coder-small", "helper-q4", "Helper-Q4"}


def test_only_active_leases_are_read() -> None:
    store = Store([lease((ledger.MODEL, "coder-q3"))])
    pool = unexpiring(PoolService(
        config=PoolConfig(path=Path("pool.yaml"), swap=SWAP),
        open_store=lambda: contextlib.nullcontext(store),
        pool_token="token",
        config_root=Path("piconfig"),
    ))

    pool.held_models()

    assert store.asked == [ledger.ACTIVE]


def test_nothing_is_held_with_no_lease_out() -> None:
    assert service([]).held_models() == frozenset()


def test_a_pool_with_no_llama_swap_file_holds_the_ids_alone() -> None:
    held = service([lease((ledger.MODEL, "coder-q3"))], swap=None).held_models()

    assert held == {"coder-q3"}


def test_with_no_pool_in_force_nothing_is_held() -> None:
    assert held_models(None) == frozenset()


def test_the_pool_in_force_is_asked() -> None:
    assert held_models(service([lease((ledger.MODEL, "helper-q4"))])) == {
        "helper-q4", "Helper-Q4",
    }


def test_the_daemons_bridge_refuses_a_completion_while_nothing_is_held(
    tmp_path: Path, monkeypatch
) -> None:
    # A closed port upstream: a completion the filter let through would fail
    # there, and never reach the rig.
    monkeypatch.setenv(events.URL_ENV, "http://127.0.0.1:1")
    root = tmp_path / "root"
    root.mkdir()
    pool_dir(root).mkdir()

    with TestClient(daemon_app.create_app(root)):
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(bridge.socket_path(root))),
            base_url="http://bridge",
        ) as member:
            answer = member.post("/v1/chat/completions", json={"model": "model-a"})

    assert answer.status_code == bridge.REFUSED
    assert "'model-a' is not a model the pool holds now" in answer.json()["error"]["message"]
