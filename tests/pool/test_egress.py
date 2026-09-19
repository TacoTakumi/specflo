"""The egress ceiling: which members a request may be served by.

A member's egress class says where data sent to it goes: it stays on this
host, it goes to a provider that neither keeps nor trains on it, or it goes to
one that may. A request is served only by a member whose class is not more
open than the request's ceiling, and the ceiling is the strictest of the class
the request names and the class the pool's definition accepts. A request that
names none gets no-train, so private code reaches a provider that may train
on it only when someone asked for that in so many words.

A pool that holds no member under the ceiling can never serve the request, so
it is refused at once, with the classes named, and never waits. A pool that
holds one that is busy is full to the request, however many more open members
stand free: it waits for the member it may have.

The module's own tests are of plain values and the ledger's of rows. The
service's tests run real processes: the stub pi under an agent host, as in
the grant's tests. One test asks through the verb, of a real daemon.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest
import yaml

from specflo.cli import app
from specflo.daemon.poolstore import Lease
from specflo.errors import SpecfloError
from specflo.pool import cli_admin, egress, ledger, service, waiting
from specflo.pool import config as pool_config
from specflo.pool.config import Account, Member, Pool, PoolConfig
from specflo.pool.definitions import DEFAULT_EGRESS, EGRESS_CLASSES

from . import test_lease_request
from .test_lease_request import checkout, pool_daemon, runner  # noqa: F401 - fixtures
from .test_runner import DEFINITION

SRC = Path(__file__).resolve().parents[2] / "src" / "specflo"


# -- the classes and the ceiling: plain values --------------------------------


def test_the_classes_run_from_the_strictest_to_the_most_open():
    assert EGRESS_CLASSES == ("local", "no-train", "open")
    assert egress.strictest(["open", "local", "no-train"]) == "local"
    assert egress.strictest(["open", "no-train"]) == "no-train"
    assert egress.strictest(["open"]) == "open"


@pytest.mark.parametrize(
    "asked, accepted, ceiling",
    [
        (None, "open", "no-train"),
        (None, "no-train", "no-train"),
        (None, "local", "local"),
        ("open", "open", "open"),
        ("open", "no-train", "no-train"),
        ("open", "local", "local"),
        ("local", "open", "local"),
        ("no-train", "open", "no-train"),
    ],
)
def test_the_ceiling_is_the_strictest_of_what_is_asked_and_what_the_definition_accepts(
    asked, accepted, ceiling
):
    assert egress.ceiling(asked, [accepted]) == ceiling


def test_a_request_that_names_no_class_gets_the_default():
    assert DEFAULT_EGRESS == "no-train"
    assert egress.ceiling(None, []) == DEFAULT_EGRESS
    assert egress.ceiling("open", []) == "open"


def test_every_further_limit_can_only_make_the_ceiling_stricter():
    assert egress.ceiling("open", ["open", "open"]) == "open"
    assert egress.ceiling("open", ["open", "local"]) == "local"
    assert egress.ceiling(None, ["open", "open"]) == "no-train"


def test_a_member_is_allowed_when_its_class_is_not_more_open_than_the_ceiling():
    assert [egress.allowed(c, "no-train") for c in EGRESS_CLASSES] == [True, True, False]
    assert [egress.allowed(c, "local") for c in EGRESS_CLASSES] == [True, False, False]
    assert all(egress.allowed(c, "open") for c in EGRESS_CLASSES)
    assert egress.within("no-train") == ("local", "no-train")
    assert egress.within("local") == ("local",)
    assert egress.within("open") == EGRESS_CLASSES


def test_a_class_that_is_not_one_is_refused_with_the_classes_named():
    for call in (
        lambda: egress.ceiling("public", ["open"]),
        lambda: egress.ceiling(None, ["public"]),
        lambda: egress.allowed("public", "open"),
        lambda: egress.within("public"),
    ):
        with pytest.raises(egress.UnknownClass, match="public") as refused:
            call()
        assert isinstance(refused.value, SpecfloError)
        assert all(name in str(refused.value) for name in EGRESS_CLASSES)


# -- the ledger: members more open than the request accepts are passed over ----


def local(name: str) -> Member:
    return Member(
        name=name, command="pi", backing="local", labels=(), capacity=1,
        egress="local", model="tc3",
    )


def hosted(name: str, egress_class: str) -> Member:
    return Member(
        name=name, command="pi", backing="hosted", labels=(), capacity=1,
        egress=egress_class, model="some-vendor/some-model", account="shared",
    )


def configured(members: list[Member], size: int | None = None) -> PoolConfig:
    rebasers = Pool(
        name="rebasers", definition="rebaser", members=tuple(m.name for m in members),
        size=len(members) if size is None else size, idle_default=600, idle_max=3600,
    )
    return PoolConfig(
        path=Path("pool.yaml"), accounts=(Account(name="shared", cap=5, key_env="SHARED_KEY"),),
        members=tuple(members), pools=(rebasers,),
    )


def lease_of(placement: ledger.Placement, n: int) -> Lease:
    """The row a grant writes for *placement*."""
    taken = dict((r.kind, r.name) for r in placement.resources)
    return Lease(
        id=f"lease-{n}", team_lease_id=None, holder_hash="h", holder_label="a",
        member=taken["member"], pool=taken["pool"], resources=placement.resources,
        acquired="2026-03-01T12:00:00.000+00:00",
        last_activity="2026-03-01T12:00:00.000+00:00", idle_limit=600, state="active",
    )


def asking(limit: str) -> ledger.Request:
    return ledger.Request(pool="rebasers", egress=egress.within(limit))


def test_the_ledger_passes_over_a_member_more_open_than_the_request_accepts():
    config = configured([hosted("open-1", "open"), hosted("hosted-1", "no-train"), local("m1")])

    assert ledger.place(config, [], asking("open")).member.name == "open-1"
    assert ledger.place(config, [], asking("no-train")).member.name == "hosted-1"
    assert ledger.place(config, [], asking("local")).member.name == "m1"


def test_a_pool_with_no_member_the_request_accepts_is_not_full_but_refused_for_good():
    config = configured([hosted("open-1", "open")])

    with pytest.raises(ledger.NoMemberAllowed, match="rebasers") as refused:
        ledger.place(config, [], asking("no-train"))

    # not the refusal a request waits on
    assert not isinstance(refused.value, ledger.NoRoom)
    assert isinstance(refused.value, SpecfloError)
    message = str(refused.value)
    assert "'open-1'" in message and "'open'" in message
    assert "'local'" in message and "'no-train'" in message


def test_the_refusal_for_good_comes_before_any_count_of_what_is_full():
    config = configured([hosted("open-1", "open")])
    out = [lease_of(ledger.place(config, [], asking("open")), 1)]

    with pytest.raises(ledger.NoRoom):
        ledger.place(config, out, asking("open"))
    with pytest.raises(ledger.NoMemberAllowed):
        ledger.place(config, out, asking("no-train"))


def test_a_busy_member_under_the_ceiling_is_waited_for_beside_a_free_one_above_it():
    config = configured([local("m1"), hosted("open-1", "open")])
    out = [lease_of(ledger.place(config, [], asking("no-train")), 1)]

    with pytest.raises(ledger.NoRoom) as full:
        ledger.place(config, out, asking("no-train"))

    # the member the request may not have is not said to be full
    assert "'m1'" in str(full.value)
    assert "'open-1'" not in str(full.value).split(" - ")[0]
    assert ledger.place(config, out, asking("open")).member.name == "open-1"


# -- the service: real members under the stub pi -------------------------------

ACCEPTS_OPEN = dataclasses.replace(DEFINITION, egress="open")


def open_member(pool_rig) -> Member:
    return dataclasses.replace(pool_rig.hosted_member(), name="open-1", egress="open")


def pool_of(pool_rig, *members: Member, definition=ACCEPTS_OPEN) -> PoolConfig:
    """The pool "rebasers" of *members*, under a definition that accepts class open."""
    return dataclasses.replace(pool_rig.config(*members), definitions=(definition,))


def waiting_ids(pool_rig) -> list[str]:
    with pool_rig.store() as store:
        return [row.id for row in store.list_waiting()]


def test_a_default_request_to_a_pool_of_one_open_member_is_refused_naming_the_classes(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, open_member(pool_rig)))

    with pytest.raises(service.EgressRefused, match="rebasers") as refused:
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    message = str(refused.value)
    assert "'open-1'" in message and "'open'" in message and "'no-train'" in message
    assert not isinstance(refused.value, service.NoFreeMember)
    with pool_rig.store() as store:
        assert store.list_leases() == []
    assert pool_rig.pane_names() == []


def test_the_refused_request_never_waits_however_long_it_may(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, open_member(pool_rig)))
    asked = waiting.Waiting(
        svc, "rebasers", holder_label="a", cwd=pool_rig.work, wait=3600,
        mint_id=lambda: "request-a",
    )

    with pytest.raises(service.EgressRefused):
        asked.attempt()

    assert waiting_ids(pool_rig) == []
    asked.leave()
    assert pool_rig.pane_names() == []


def test_the_same_request_with_the_class_open_is_granted(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, open_member(pool_rig)))

    granted = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work, egress="open")

    assert granted.agent == "open-1"
    svc.end_lease(granted.lease_id, "released")


def test_a_waiting_request_carries_its_class_to_the_grant(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, open_member(pool_rig)))
    asked = waiting.Waiting(
        svc, "rebasers", holder_label="a", cwd=pool_rig.work, wait=3600, egress="open",
    )

    granted = asked.attempt()

    assert granted.agent == "open-1"
    svc.end_lease(granted.lease_id, "released")


def test_no_request_widens_what_the_definition_accepts(pool_rig):
    # an in-memory configuration; the pool file's check refuses this pool outright
    svc = pool_rig.service(pool_of(pool_rig, open_member(pool_rig), definition=DEFINITION))

    with pytest.raises(service.EgressRefused, match="rebaser") as refused:
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work, egress="open")

    assert "'no-train'" in str(refused.value)
    assert pool_rig.pane_names() == []


def test_a_class_that_is_not_one_is_refused_by_the_service(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, open_member(pool_rig)))

    with pytest.raises(egress.UnknownClass, match="public"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work, egress="public")

    assert pool_rig.pane_names() == []


@pytest.mark.parametrize("listed", ["local first", "open first"])
def test_a_default_request_always_gets_the_local_member_and_waits_for_it_when_it_is_busy(
    pool_rig, listed
):
    members = [pool_rig.local_member(), open_member(pool_rig)]
    if listed == "open first":
        members.reverse()
    svc = pool_rig.service(pool_of(pool_rig, *members))

    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert first.agent == "local-1"

    # the open member stands free, and the pool has a slot for it
    asked = waiting.Waiting(
        svc, "rebasers", holder_label="b", cwd=pool_rig.work, wait=3600,
        mint_id=lambda: "request-b",
    )
    assert asked.attempt() is None
    assert waiting_ids(pool_rig) == ["request-b"]
    assert pool_rig.pane_names() == ["local-1"]
    with pytest.raises(service.NoFreeMember, match="local-1"):
        svc.grant("rebasers", holder_label="c", cwd=pool_rig.work, waiting_id="request-b")

    svc.end_lease(first.lease_id, "released")
    second = asked.attempt()

    assert second.agent == "local-1"
    assert waiting_ids(pool_rig) == []
    with pool_rig.store() as store:
        assert {row.member for row in store.list_leases()} == {"local-1"}
    svc.end_lease(second.lease_id, "released")


def test_a_default_request_that_waits_holds_up_no_request_for_the_open_member(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, pool_rig.local_member(), open_member(pool_rig)))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    asked = waiting.Waiting(svc, "rebasers", holder_label="b", cwd=pool_rig.work, wait=3600)
    assert asked.attempt() is None

    # it came later, and what it may have the waiting request may not
    later = svc.grant("rebasers", holder_label="c", cwd=pool_rig.work, egress="open")

    assert later.agent == "open-1"
    assert asked.attempt() is None
    asked.leave()
    for grant in (first, later):
        svc.end_lease(grant.lease_id, "released")


# -- the verb, asked of a real daemon -----------------------------------------

# The lease verb's own pool: one local member under a definition of class local.
WRITE_POOL = test_lease_request.write_pool
ACCEPTING_OPEN = test_lease_request.REBASER.replace("egress: local", "egress: open")


def write_open_pool(rig) -> None:
    """The lease verb's pool with a hosted member of class open beside the
    local one, under a definition that accepts it."""
    WRITE_POOL(rig)
    directory = cli_admin.pool_dir(rig.root)
    (directory / pool_config.DEFINITIONS_DIR / "rebaser.md").write_text(
        ACCEPTING_OPEN, encoding="utf-8"
    )
    path = directory / pool_config.POOL_FILE
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["accounts"] = [{"name": "team-a", "cap": 2, "key_env": "TEAM_A_KEY"}]
    data["members"].append({
        "name": "open-1", "command": rig.command, "backing": "hosted",
        "model": "some-vendor/some-model", "account": "team-a", "labels": [],
        "capacity": 1, "egress": "open",
    })
    data["pools"][0].update(members=["local-1", "open-1"], size=2)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def open_pool(monkeypatch):
    """Asked for before the daemon: the daemon's fixture writes this pool instead."""
    monkeypatch.setattr(test_lease_request, "write_pool", write_open_pool)


def test_the_verb_asks_with_the_class_its_option_names(open_pool, checkout, pool_rig):  # noqa: F811
    first = runner.invoke(app, ["lease", "request", "rebasers", "--json"])
    assert first.exit_code == 0, first.output
    assert json.loads(first.stdout)["agent"] == "local-1"

    # the local member is busy and the open one free: a default request does not fall to it
    full = runner.invoke(app, ["lease", "request", "rebasers", "--wait", "0"])
    assert full.exit_code != 0
    assert "local-1" in full.output

    unknown = runner.invoke(app, ["lease", "request", "rebasers", "--egress", "public"])
    assert unknown.exit_code != 0
    assert "public" in unknown.output and "no-train" in unknown.output

    opened = runner.invoke(app, ["lease", "request", "rebasers", "--egress", "open", "--json"])
    assert opened.exit_code == 0, opened.output
    assert json.loads(opened.stdout)["agent"] == "open-1"
    with pool_rig.store() as store:
        assert sorted(row.member for row in store.list_leases(state="active")) == [
            "local-1", "open-1",
        ]


# -- structure ----------------------------------------------------------------


def test_the_service_hands_the_ledger_the_classes_with_every_request():
    tree = ast.parse((SRC / "pool" / "service.py").read_text(encoding="utf-8"))
    requests = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Request"
    ]

    assert len(requests) == 1
    assert {keyword.arg for keyword in requests[0].keywords} == {"pool", "egress"}


def test_the_egress_module_is_plain_values_and_starts_nothing():
    tree = ast.parse((SRC / "pool" / "egress.py").read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add("." * node.level + (node.module or ""))
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)

    assert modules <= {"__future__", "collections.abc", "..errors", ".definitions"}
