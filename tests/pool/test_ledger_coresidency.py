"""The rig's cards are one more thing the ledger counts: which models are leased together.

A local member runs one model of the rig's llama-swap configuration, and
llama-swap keeps loaded side by side only the models one expansion of one
matrix set holds. So a lease on a local member takes its model, the lease row
says so, and a local member fits only when its model may be loaded beside
every model that is under lease, in whichever pool. A model is always at home
beside itself. A request whose model has no place now waits, as it waits for
a full member: a lease ending is what makes room. A hosted member runs on no
card of the rig: it is not checked and its lease takes no model.

It is the same function that answers, from the same rows. The fixture matrix
has the one set ``(a | b) & c``, and a model ``d`` that is in no set.

The pool reads the llama-swap configuration and the event stream and asks
llama-swap for nothing else: what is loaded, what is unloaded, which profile
is active and which card a model runs on are llama-swap's own. The last tests
scan the whole pool package for the requests that would say otherwise.

The ledger's own tests hand it rows and read its answer. The service's tests
run real processes: the stub pi under an agent host.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from specflo.daemon.poolstore import Lease, Resource
from specflo.pool import ledger, matrix, service, waiting
from specflo.pool.config import Account, Member, Pool, PoolConfig

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "pool" / "llama-swap.yaml"
PACKAGE = Path(__file__).resolve().parents[2] / "src" / "specflo" / "pool"


def local(name: str, model: str, capacity: int = 1) -> Member:
    return Member(
        name=name, command="pi", backing="local", labels=(), capacity=capacity,
        egress="local", model=model,
    )


def hosted(name: str) -> Member:
    return Member(
        name=name, command="pi", backing="hosted", labels=(), capacity=1,
        egress="no-train", model="some-vendor/some-model", account="shared",
    )


def pool(name: str, *members: str, size: int = 4) -> Pool:
    return Pool(
        name=name, definition="rebaser", members=members, size=size,
        idle_default=600, idle_max=3600,
    )


def configured(members: list[Member], pools: list[Pool]) -> PoolConfig:
    return PoolConfig(
        path=Path("pool.yaml"), accounts=(Account(name="shared", cap=4, key_env="SHARED_KEY"),),
        members=tuple(members), pools=tuple(pools), llama_swap=FIXTURE,
        swap=matrix.read(FIXTURE),
    )


def one_pool_a_model() -> PoolConfig:
    """A pool for each model of the fixture, of the one member that runs it."""
    return configured(
        [local(f"m-{n}", f"model-{n}") for n in "abcd"],
        [pool(f"on-{n}", f"m-{n}") for n in "abcd"],
    )


def lease_of(placement: ledger.Placement, n: int, *, state: str = "active") -> Lease:
    """The row a grant writes for *placement*."""
    taken = dict((r.kind, r.name) for r in placement.resources)
    return Lease(
        id=f"lease-{n}", team_lease_id=None, holder_hash="h", holder_label="a",
        member=taken["member"], pool=taken["pool"], resources=placement.resources,
        acquired="2026-03-01T12:00:00.000+00:00",
        last_activity="2026-03-01T12:00:00.000+00:00", idle_limit=600, state=state,
    )


def leased(config: PoolConfig, *pools: str) -> list[Lease]:
    """The rows of one lease granted in each of *pools*, in that order."""
    out: list[Lease] = []
    for n, name in enumerate(pools, start=1):
        out.append(lease_of(ledger.place(config, out, ledger.Request(pool=name)), n))
    return out


def kinds(placement: ledger.Placement) -> set[tuple[str, str]]:
    return {(r.kind, r.name) for r in placement.resources}


# -- the ledger: rows in, an answer out ---------------------------------------


def test_a_lease_on_a_local_member_takes_its_model():
    config = one_pool_a_model()

    placed = ledger.place(config, [], ledger.Request(pool="on-a"))

    assert kinds(placed) == {("pool", "on-a"), ("member", "m-a"), ("model", "model-a")}


def test_leases_on_two_models_of_one_combination_coexist():
    config = one_pool_a_model()

    out = leased(config, "on-a", "on-c")

    assert [row.member for row in out] == ["m-a", "m-c"]
    # and b beside c, as a is
    beside_c = ledger.place(config, leased(config, "on-c"), ledger.Request(pool="on-b"))
    assert beside_c.member.name == "m-b"


def test_a_request_for_a_model_that_is_either_or_with_a_leased_one_waits_until_it_is_released():
    config = one_pool_a_model()
    (on_a,) = leased(config, "on-a")

    # "on-b" has granted nothing and its member is free
    with pytest.raises(ledger.NoRoom, match="model 'model-b'") as refused:
        ledger.place(config, [on_a], ledger.Request(pool="on-b"))

    assert "model-a" in str(refused.value)
    assert "on-b" in str(refused.value)
    # it is a matter of room: a lease ending makes it fit
    assert not isinstance(refused.value, ledger.NoMemberAllowed)
    released = dataclasses.replace(on_a, state="released")
    assert ledger.place(config, [released], ledger.Request(pool="on-b")).member.name == "m-b"


def test_two_leases_on_one_model_coexist():
    config = configured(
        [local("m1", "model-a", capacity=2), local("m2", "model-a")],
        [pool("rebasers", "m1"), pool("critics", "m2")],
    )

    out = leased(config, "rebasers", "rebasers", "critics")

    assert [ledger.agent_of(row) for row in out] == ["m1", "m1.2", "m2"]
    assert all(Resource("model", "model-a") in row.resources for row in out)


def test_a_model_in_no_set_with_a_leased_model_waits():
    config = one_pool_a_model()

    with pytest.raises(ledger.NoRoom, match="model 'model-d'") as refused:
        ledger.place(config, leased(config, "on-c"), ledger.Request(pool="on-d"))
    assert "model-c" in str(refused.value)
    # and the other way round: nothing is loaded beside a model that runs alone
    with pytest.raises(ledger.NoRoom, match="model 'model-c'") as refused:
        ledger.place(config, leased(config, "on-d"), ledger.Request(pool="on-c"))
    assert "model-d" in str(refused.value)
    # alone it runs
    assert ledger.place(config, [], ledger.Request(pool="on-d")).member.name == "m-d"


def test_the_models_are_checked_all_together_and_the_refusal_names_the_ones_in_the_way():
    config = one_pool_a_model()
    out = leased(config, "on-a", "on-c")

    with pytest.raises(ledger.NoRoom, match="model 'model-b'") as refused:
        ledger.place(config, out, ledger.Request(pool="on-b"))

    # b is at home beside c; what keeps it off the rig is a
    assert "model-a" in str(refused.value)
    assert "model-c" not in str(refused.value)


def test_models_that_fit_two_by_two_and_not_all_together_do_not_fit(tmp_path):
    path = tmp_path / "llama-swap.yaml"
    path.write_text(
        "models:\n  model-a: {cmd: a}\n  model-b: {cmd: b}\n  model-c: {cmd: c}\n"
        "matrix:\n  vars: {a: model-a, b: model-b, c: model-c}\n"
        '  sets:\n    ab: "a & b"\n    bc: "b & c"\n    ac: "a & c"\n',
        encoding="utf-8",
    )
    config = dataclasses.replace(one_pool_a_model(), swap=matrix.read(path))

    with pytest.raises(ledger.NoRoom, match="model 'model-c'") as refused:
        ledger.place(config, leased(config, "on-a", "on-b"), ledger.Request(pool="on-c"))

    # neither is in the way by itself, so both are named
    assert "model-a" in str(refused.value) and "model-b" in str(refused.value)


def test_a_member_whose_model_has_no_place_is_passed_over_for_a_listed_member_that_fits():
    config = configured(
        [local("m-a", "model-a"), local("m-b", "model-b"), local("m-c", "model-c")],
        [pool("rebasers", "m-a"), pool("critics", "m-b", "m-c")],
    )

    placed = ledger.place(config, leased(config, "rebasers"), ledger.Request(pool="critics"))

    # m-b is free, and its model is not
    assert placed.member.name == "m-c"
    assert kinds(placed) == {("pool", "critics"), ("member", "m-c"), ("model", "model-c")}


def test_a_full_member_is_not_put_down_to_its_model():
    config = configured(
        [local("m-a", "model-a")], [pool("rebasers", "m-a"), pool("critics", "m-a")]
    )

    with pytest.raises(ledger.NoRoom, match="m-a") as refused:
        ledger.place(config, leased(config, "rebasers"), ledger.Request(pool="critics"))

    assert "model" not in str(refused.value)


def test_a_hosted_member_is_not_checked_and_its_lease_takes_no_model():
    config = configured(
        [local("m-a", "model-a"), local("m-d", "model-d"), hosted("h1"), hosted("h2")],
        [pool("on-a", "m-a"), pool("on-d", "m-d"), pool("hosted", "h1", "h2")],
    )

    # beside a model that runs alone, and beside another hosted lease
    out = leased(config, "on-d", "hosted", "hosted")

    assert [row.member for row in out] == ["m-d", "h1", "h2"]
    assert {(r.kind, r.name) for r in out[1].resources} == {
        ("pool", "hosted"), ("member", "h1"), ("account", "shared"),
    }
    # nor is a hosted lease in the way of a model: its model is no model of the rig
    assert ledger.place(config, out[1:], ledger.Request(pool="on-a")).member.name == "m-a"


def test_the_models_are_counted_from_what_the_active_rows_say_they_took():
    config = one_pool_a_model()
    placed = ledger.place(config, [], ledger.Request(pool="on-a"))
    ended = lease_of(placed, 1, state="released")
    # a row whose columns name nothing declared, and whose resources name the model
    odd = dataclasses.replace(lease_of(placed, 2), member="elsewhere", pool="elsewhere")
    # a row from before a lease took its model: it says nothing of a model
    older = dataclasses.replace(
        lease_of(placed, 3), resources=(Resource("pool", "on-a"), Resource("member", "m-a")),
    )

    assert ledger.place(config, [ended], ledger.Request(pool="on-b")).member.name == "m-b"
    with pytest.raises(ledger.NoRoom, match="model 'model-b'"):
        ledger.place(config, [odd], ledger.Request(pool="on-b"))
    # nothing is read into it from the member it names
    assert ledger.place(config, [older], ledger.Request(pool="on-b")).member.name == "m-b"


def test_a_leased_model_the_configuration_no_longer_has_runs_alone():
    config = one_pool_a_model()
    (on_a,) = leased(config, "on-a")
    kept = tuple(r for r in on_a.resources if r.kind != "model")
    gone = dataclasses.replace(on_a, resources=(*kept, Resource("model", "model-gone")))

    with pytest.raises(ledger.NoRoom, match="model-gone"):
        ledger.place(config, [gone], ledger.Request(pool="on-c"))


def test_a_configuration_with_no_llama_swap_configuration_checks_and_takes_no_model():
    config = dataclasses.replace(one_pool_a_model(), llama_swap=None, swap=None)

    out = leased(config, "on-a", "on-b", "on-d")

    assert [row.member for row in out] == ["m-a", "m-b", "m-d"]
    assert all(r.kind != "model" for row in out for r in row.resources)


# -- the service: two local members whose models are either-or ----------------


def either_or(pool_rig) -> PoolConfig:
    """One pool, "rebasers", of size 2: local-a runs model-a and local-b model-b."""
    members = [
        dataclasses.replace(pool_rig.local_member(), name=f"local-{n}", model=f"model-{n}")
        for n in "ab"
    ]
    return dataclasses.replace(
        pool_rig.config(*members), llama_swap=FIXTURE, swap=matrix.read(FIXTURE)
    )


def waiting_ids(pool_rig) -> list[str]:
    with pool_rig.store() as store:
        return [row.id for row in store.list_waiting()]


def test_a_lease_rows_resources_name_the_model_it_took(pool_rig):
    svc = pool_rig.service(either_or(pool_rig))

    granted = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pool_rig.store() as store:
        row = store.get_lease(granted.lease_id)
    assert {(r.kind, r.name) for r in row.resources} == {
        ("pool", "rebasers"), ("member", "local-a"), ("model", "model-a"),
    }
    svc.end_lease(granted.lease_id, "released")


def test_a_request_for_the_other_model_waits_and_is_granted_when_the_first_is_released(pool_rig):
    config = either_or(pool_rig)
    assert config.pools[0].size == 2
    svc = pool_rig.service(config)
    first = svc.grant("rebasers", holder_label="orchestrator-a", cwd=pool_rig.work)
    asked = waiting.Waiting(
        svc, "rebasers", holder_label="orchestrator-b", cwd=pool_rig.work, wait=3600,
        mint_id=lambda: "request-b",
    )

    # the pool has a slot free and local-b serves nothing
    assert asked.attempt() is None
    assert waiting_ids(pool_rig) == ["request-b"]
    assert pool_rig.pane_names() == ["local-a"]

    svc.end_lease(first.lease_id, "released")
    granted = asked.attempt()

    # local-a is free again and listed first
    assert granted.agent == "local-a"
    assert waiting_ids(pool_rig) == []
    svc.end_lease(granted.lease_id, "released")


def test_the_refusal_names_the_model_and_the_leased_model_in_its_way(pool_rig):
    svc = pool_rig.service(either_or(pool_rig))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pytest.raises(service.NoFreeMember, match="model 'model-b'") as refused:
        svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)

    assert "model-a" in str(refused.value)
    svc.end_lease(first.lease_id, "released")


# -- structure: what the pool never asks of llama-swap ------------------------

# llama-swap has no request that loads a model by name: a model is loaded by
# the first request sent to it, on one of these paths or under /upstream/.
LOADS = (
    "/upstream", "/v1/chat/completions", "/v1/completions", "/v1/responses", "/v1/messages",
    "/v1/embeddings", "/v1/rerank", "/v1/reranking", "/rerank", "/reranking", "/infill",
    "/completion", "/props", "/v1/audio", "/v1/images", "/sdapi", "/audioapi", "/comfyui",
)
# GET /unload, POST /api/models/unload and POST /api/models/unload/<model>
UNLOADS = ("/unload", "/api/models")
# GET /api/profiles and PUT /api/profiles/active
PROFILES = ("/api/profiles",)
# llama-swap has no request that puts a model on a card: the cards are set in
# its configuration, by these in a model's command or environment.
CARDS = (
    "VISIBLE_DEVICES", "--device", "--main-gpu", "--tensor-split", "--split-mode",
    "-ngl", "--n-gpu-layers",
)
# Every llama-swap request that changes something is one of these, but GET /unload.
WRITING_VERBS = ("POST", "PUT", "PATCH", "DELETE")

# The bridge is the one module that carries a request to llama-swap, and the
# request is a member's own, on a path the member may ask for. It names the
# completion paths and the method they are made with because that is its
# allow list, so the two text scans below leave it out. What it may name and
# may not is checked where the filter is: test_bridge_filter.py.
RELAYS = "bridge.py"


def modules() -> dict[str, ast.Module]:
    found = {
        path.name: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGE.rglob("*.py"))
    }
    # the scan has its files
    assert {"ledger.py", "matrix.py", "events.py", "service.py"} <= set(found)
    return found


def texts(tree: ast.Module) -> list[str]:
    """Every piece of text in *tree*: the docstrings and the f-string pieces too."""
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_no_module_of_the_pool_names_a_llama_swap_load_unload_profile_or_card_request():
    named = {
        (name, mark)
        for name, tree in modules().items() if name != RELAYS
        for text in texts(tree)
        for mark in (*LOADS, *UNLOADS, *PROFILES, *CARDS) if mark in text
    }

    assert named == set()


def test_no_module_of_the_pool_sends_a_request_that_writes():
    verbs = {verb.lower() for verb in WRITING_VERBS}
    sent = set()
    for name, tree in modules().items():
        if name != RELAYS:
            sent.update((name, text) for text in texts(tree) if text.strip().lower() in verbs)
        sent.update(
            (name, ast.unparse(node.func)) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in verbs
        )

    assert sent == set()


def test_one_module_knows_where_llama_swap_is_and_one_reads_its_configuration():
    found = modules()
    knows = {name for name, tree in found.items() if "SPECFLO_LLAMA_SWAP_URL" in texts(tree)}
    # the module whose one request, the GET of the event stream, its own tests pin
    assert knows == {"events.py"}
    # the configuration is read where the matrix is, and written nowhere there
    called = {
        node.func.attr for node in ast.walk(found["matrix.py"])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "read_text" in called
    assert not called & {"write_text", "write_bytes", "open", "dump", "safe_dump", "unlink"}
