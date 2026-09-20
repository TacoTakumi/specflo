"""A hosted plan's ``- Needs:`` line that names a daemon pool asks for that pool.

A plan counts its own pools: a ``## Pools`` section, a slot to each task under
way. That count is one plan's, and two projects on one daemon would both claim
the same members by it. So for a project a daemon hosts, a Needs name that is
one of the daemon's pools is read against the daemon: the task is ready while
the pool has a lease to give, and held back while every lease is out, whatever
the plan declares for that name. A name that is neither the daemon's nor the
plan's is a fault ``validate plan`` reports. A plan in a checkout is read as
it always was, and no pool code is loaded to read it.

The plan module's side is tested on a mapping of what the pools have out. The
whole path is tested against a real daemon on a loopback port, with the stub
pi as the member a lease starts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from typer.testing import CliRunner

from specflo import config, markdown, plan, projects, spec
from specflo.cli import app
from specflo.daemon.poolstore import Lease, Resource, open_pool_store
from specflo.pool import cli_admin, planneeds
from specflo.pool import config as pool_config

from . import test_lease_request
from .conftest import START
from .test_lease_request import checkout, pool_daemon  # noqa: F401  (fixtures)
from .test_runner import pid_alive

runner = CliRunner()

POOL = "small-worker"


def _entry(tid, needs=None, progress="pending"):
    lines = [f"### {tid} — task {tid}", "- Acceptance: a", "- Verify: v", "- Implements: REQ-01"]
    if needs:
        lines.append(f"- Needs: {needs}")
    lines += [f"- Progress: {progress}", "- Status: active"]
    return "\n".join(lines) + "\n"


def _write_plan(root, cfg, slug, entries, pools=()):
    """A plan for *slug* under *root*: one requirement, *pools* declared, *entries* as its tasks."""
    spec.start_spec(root, cfg, slug, today="2026-06-22")
    spec.add_requirement(root, cfg, slug, "req", acceptance="ok", today="2026-06-22")
    plan.start_plan(root, cfg, slug, today="2026-06-22")
    for name, size in pools:
        plan.add_pool(root, cfg, slug, name, size)
    path = plan.plan_path(root, cfg, slug)
    doc = path.read_text()
    for entry in entries:
        doc = markdown.append_to_section(doc, "## Tasks", entry)
    path.write_text(doc)


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "plans"
    root.mkdir()
    config.init_config(root)
    return root


@pytest.fixture
def cfg(root):
    return config.load_config(root)


@pytest.fixture
def project(root, cfg):
    projects.create_project(root, cfg, "My Thing", created="2026-06-15")
    return "my-thing"


def _frontier(root, cfg, project, daemon_pools):
    found = plan.frontier(root, cfg, project, daemon_pools=daemon_pools)
    return {t["id"] for t in found["tasks"] if t["ready"]}, found["pools"]


# --- the ready set, on what the daemon's pools have out ---------------------


def test_a_daemon_pool_with_a_lease_to_give_admits_the_needer_whatever_the_plan_declares(
    root, cfg, project
):
    # By the plan's own count the pool is full: one slot, and T-01 holds it.
    _write_plan(root, cfg, project, [
        _entry("T-01", POOL, progress="in_progress"), _entry("T-02", POOL),
    ], pools=[(POOL, 1)])
    assert _frontier(root, cfg, project, None)[0] == set()

    ready, pools = _frontier(root, cfg, project, {POOL: {"size": 2, "in_use": 1}})

    assert ready == {"T-02"}
    assert pools == {POOL: {"size": 2, "holders": ["T-01"], "in_use": 1}}
    progress = plan.plan_progress(root, cfg, project, daemon_pools={POOL: {"size": 2, "in_use": 1}})
    assert progress["next_actionable"] == ["T-02"]


def test_a_full_daemon_pool_holds_the_needer_back_whatever_the_plan_declares(root, cfg, project):
    # By the plan's own count there is room: five slots and none held.
    _write_plan(root, cfg, project, [_entry("T-01", POOL), _entry("T-02")], pools=[(POOL, 5)])
    assert _frontier(root, cfg, project, None)[0] == {"T-01", "T-02"}

    ready, pools = _frontier(root, cfg, project, {POOL: {"size": 1, "in_use": 1}})

    assert ready == {"T-02"}
    assert pools == {POOL: {"size": 1, "holders": [], "in_use": 1}}


def test_a_pool_the_daemon_does_not_have_is_still_counted_by_the_plan(root, cfg, project):
    _write_plan(root, cfg, project, [
        _entry("T-01", "gpu:3090", progress="in_progress"), _entry("T-02", "gpu:3090"),
        _entry("T-03", "gpu:3090, " + POOL),
    ], pools=[("gpu:3090", 2)])

    ready, pools = _frontier(root, cfg, project, {POOL: {"size": 1, "in_use": 0}})

    assert ready == {"T-02", "T-03"}
    assert pools["gpu:3090"] == {"size": 2, "holders": ["T-01"]}

    plan.start_task(root, cfg, project, "T-02")
    assert _frontier(root, cfg, project, {POOL: {"size": 1, "in_use": 0}})[0] == set()


# --- validation in hosted mode ----------------------------------------------


def test_hosted_validation_reports_a_needs_name_that_is_no_pool_anywhere(root, cfg, project):
    _write_plan(root, cfg, project, [
        _entry("T-01", POOL), _entry("T-02", "gpu:3090, user"), _entry("T-03", "nopool"),
    ], pools=[("gpu:3090", 2)])
    before = plan.validate_plan(root, cfg, project)

    issues = plan.validate_plan(root, cfg, project, daemon_pools={POOL: {"size": 1, "in_use": 1}})

    (unknown,) = [issue for issue in issues if issue not in before]
    assert "T-03" in unknown and "'nopool'" in unknown
    # nothing else changed, and a checkout's validation says nothing of it
    assert [issue for issue in issues if issue != unknown] == before
    assert not any("nopool" in issue for issue in before)


def test_hosted_validation_on_a_daemon_with_no_pool_knows_the_plans_pools_only(root, cfg, project):
    _write_plan(root, cfg, project, [_entry("T-01", POOL), _entry("T-02", "gpu:3090")],
                pools=[("gpu:3090", 1)])

    issues = plan.validate_plan(root, cfg, project, daemon_pools={})

    assert [i for i in issues if POOL in i] and not [i for i in issues if "gpu:3090" in i]


# --- what a daemon root's pools have out ------------------------------------


def test_a_root_with_no_pool_directory_has_no_daemon_pools(tmp_path):
    assert planneeds.daemon_pools(tmp_path) == {}
    assert not list(tmp_path.iterdir())


def test_a_root_whose_pool_configuration_does_not_stand_has_no_daemon_pools(tmp_path):
    directory = cli_admin.pool_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / pool_config.POOL_FILE).write_text("pools: 7\n", encoding="utf-8")

    assert planneeds.daemon_pools(tmp_path) == {}


# --- a lease past its idle limit is not out -----------------------------------

REVIEWERS = "reviewers"
TEN_MINUTES = 600


def _at(minute: float) -> datetime:
    return START + timedelta(minutes=minute)


def _text(time: datetime) -> str:
    return time.isoformat(timespec="milliseconds")


@pytest.fixture
def pools_root(tmp_path):
    """A daemon root with two pools of one member each, and no daemon on it:
    no member has a host, so a lease's row is all that tells its activity."""
    directory = cli_admin.pool_dir(tmp_path)
    folder = directory / pool_config.DEFINITIONS_DIR
    folder.mkdir(parents=True)
    (folder / "rebaser.md").write_text(test_lease_request.REBASER, encoding="utf-8")
    shutil.copy(test_lease_request.FIXTURES / "llama-swap.yaml", directory / "llama-swap.yaml")
    members = {POOL: "local-1", REVIEWERS: "local-2"}
    data = {
        "llama_swap": "llama-swap.yaml",
        "members": [{
            "name": member, "command": "pi", "backing": "local", "model": "model-a",
            "labels": [], "capacity": 1, "egress": "local",
        } for member in members.values()],
        "pools": [{
            "name": pool, "definition": "rebaser", "members": [member],
            "size": 1, "idle_default": "10m", "idle_max": "4h",
        } for pool, member in members.items()],
    }
    (directory / pool_config.POOL_FILE).write_text(yaml.safe_dump(data), encoding="utf-8")
    return tmp_path


def _lease_out(root, pool, member, *, last_activity: float = 0, team=None) -> Lease:
    """An active lease of *pool* in the store of *root*, last touched at that minute."""
    lease = Lease(
        id=f"lease-{member}", team_lease_id=team, holder_hash="0" * 64, holder_label="gone",
        member=member, pool=pool,
        resources=(Resource("pool", pool), Resource("member", member)),
        acquired=_text(_at(0)), last_activity=_text(_at(last_activity)),
        idle_limit=TEN_MINUTES, state="active",
    )
    with open_pool_store(root) as store:
        store.add_lease(lease)
    return lease


def _host_says(monkeypatch, state: str, minute: float) -> None:
    """What the agent host of every member says of itself: its state, stamped at that minute."""
    status = {"state": state, "last_activity": _text(_at(minute))}
    monkeypatch.setattr(planneeds.runner, "status", lambda name: dict(status))


def test_a_lease_inside_its_idle_limit_is_out_and_one_past_it_is_not(pools_root):
    _lease_out(pools_root, POOL, "local-1")

    assert planneeds.daemon_pools(pools_root, now=_at(9))[POOL] == {"size": 1, "in_use": 1}
    assert planneeds.daemon_pools(pools_root, now=_at(10))[POOL] == {"size": 1, "in_use": 0}


def test_the_time_is_the_present_when_none_is_given(pools_root):
    _lease_out(pools_root, POOL, "local-1")  # last touched long ago

    assert planneeds.daemon_pools(pools_root)[POOL] == {"size": 1, "in_use": 0}


def test_the_later_of_the_rows_and_the_hosts_activity_counts(pools_root, monkeypatch):
    _lease_out(pools_root, POOL, "local-1")
    _host_says(monkeypatch, "idle", 9)

    assert planneeds.daemon_pools(pools_root, now=_at(15))[POOL]["in_use"] == 1
    assert planneeds.daemon_pools(pools_root, now=_at(19))[POOL]["in_use"] == 0


def test_a_working_member_keeps_its_lease_out(pools_root, monkeypatch):
    _lease_out(pools_root, POOL, "local-1")
    _host_says(monkeypatch, "working", 1)

    assert planneeds.daemon_pools(pools_root, now=_at(40))[POOL]["in_use"] == 1


def test_a_team_is_judged_by_its_latest_activity_across_the_pools(pools_root):
    _lease_out(pools_root, POOL, "local-1", team="team-1")
    _lease_out(pools_root, REVIEWERS, "local-2", last_activity=9, team="team-1")

    # The worker's own row is fifteen minutes old; the reviewer's keeps both.
    out = planneeds.daemon_pools(pools_root, now=_at(15))
    assert (out[POOL]["in_use"], out[REVIEWERS]["in_use"]) == (1, 1)
    out = planneeds.daemon_pools(pools_root, now=_at(19))
    assert (out[POOL]["in_use"], out[REVIEWERS]["in_use"]) == (0, 0)


def test_reading_ends_no_lease_and_stops_no_member(pools_root, monkeypatch):
    lease = _lease_out(pools_root, POOL, "local-1")
    monkeypatch.setattr(
        planneeds.runner, "stop", lambda *a, **k: pytest.fail("the reader stopped a member")
    )

    assert planneeds.daemon_pools(pools_root, now=_at(60))[POOL]["in_use"] == 0

    with open_pool_store(pools_root) as store:
        assert store.list_leases() == [lease]
        assert store.list_transitions() == []


# --- a hosted plan, read on the daemon --------------------------------------


@pytest.fixture(autouse=True)
def small_worker_pool(monkeypatch):
    """The test daemon's one pool is named as an admin would name a worker pool."""
    write_rebasers = test_lease_request.write_pool

    def write_pool(rig) -> None:
        write_rebasers(rig)
        path = cli_admin.pool_dir(rig.root) / pool_config.POOL_FILE
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["pools"][0]["name"] = POOL
        path.write_text(yaml.safe_dump(data), encoding="utf-8")

    monkeypatch.setattr(test_lease_request, "write_pool", write_pool)


@pytest.fixture
def hosted_plan(checkout, pool_daemon):  # noqa: F811
    """A project the daemon hosts, active in the checkout. Its plan declares
    five slots of the daemon's pool name, which the daemon's count overrules."""
    made = runner.invoke(app, ["new", "Needy", "--remote", "home"])
    assert made.exit_code == 0, made.output
    daemon_root = pool_daemon["root"]
    _write_plan(daemon_root, config.load_config(daemon_root), "needy", [
        _entry("T-01", POOL), _entry("T-02"), _entry("T-03", "nopool"),
    ], pools=[(POOL, 5)])
    return "needy"


def _fresh(code, cwd=None):
    """Run *code* in a fresh interpreter, where no other test has loaded a module."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, cwd=cwd
    )


def _task_list() -> dict:
    result = runner.invoke(app, ["task", "list", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _ready(listing: dict) -> set[str]:
    return {t["id"] for t in listing["tasks"] if t["ready"]}


def test_a_hosted_task_is_ready_while_the_daemon_pool_has_a_lease_to_give(hosted_plan):
    listing = _task_list()

    assert "T-01" in _ready(listing)
    assert listing["pools"][POOL] == {"size": 1, "holders": [], "in_use": 0}
    assert "T-01" in listing["progress"]["next_actionable"]


def test_a_hosted_task_is_held_back_while_every_lease_of_the_daemon_pool_is_out(hosted_plan):
    leased = runner.invoke(app, ["lease", "request", POOL, "--json"])
    assert leased.exit_code == 0, leased.output

    listing = _task_list()

    assert _ready(listing) == {"T-02", "T-03"}
    assert listing["pools"][POOL] == {"size": 1, "holders": [], "in_use": 1}
    assert "T-01" not in listing["progress"]["next_actionable"]

    released = runner.invoke(app, ["lease", "release", json.loads(leased.stdout)["lease"]])
    assert released.exit_code == 0, released.output
    assert "T-01" in _ready(_task_list())


def test_a_lease_past_its_idle_limit_does_not_hold_a_hosted_task_back(
    hosted_plan, pool_rig, monkeypatch
):
    leased = runner.invoke(app, ["lease", "request", POOL, "--json"])
    assert leased.exit_code == 0, leased.output
    with pool_rig.store() as store:
        rows, transitions = store.list_leases(), store.list_transitions()
    assert [row.state for row in rows] == ["active"]
    # The holder goes away: nothing is done on the member, and no one asks the
    # pool for anything. Only the plan is read, at a later time each read.
    read = planneeds.daemon_pools

    def read_after(**later):
        monkeypatch.setattr(
            planneeds, "daemon_pools",
            lambda root: read(root, now=datetime.now(timezone.utc) + timedelta(**later)),
        )

    read_after(minutes=9)
    listing = _task_list()
    assert "T-01" not in _ready(listing)
    assert listing["pools"][POOL] == {"size": 1, "holders": [], "in_use": 1}

    read_after(minutes=11)
    listing = _task_list()
    assert "T-01" in _ready(listing)
    assert listing["pools"][POOL] == {"size": 1, "holders": [], "in_use": 0}
    assert "T-01" in listing["progress"]["next_actionable"]

    # The reader ended nothing: the row is as it was, and the member runs.
    with pool_rig.store() as store:
        assert (store.list_leases(), store.list_transitions()) == (rows, transitions)
    assert pid_alive(pool_rig.status("local-1")["pi_pid"])


def test_validate_plan_on_a_hosted_project_reports_the_unknown_pool(hosted_plan):
    result = runner.invoke(app, ["validate", "plan"])

    assert result.exit_code == 1, result.output
    (unknown,) = [line for line in result.output.splitlines() if "nopool" in line]
    assert "T-03" in unknown
    assert POOL not in result.output


# --- a plan in a checkout is read as it always was --------------------------


@pytest.fixture
def local_plan(tmp_path, monkeypatch):
    """A plan in a checkout with no remote, and no way to ask after a daemon's pools."""
    root = tmp_path / "local"
    root.mkdir()
    config.init_config(root)
    monkeypatch.chdir(root)
    assert runner.invoke(app, ["new", "My Thing"]).exit_code == 0
    cfg = config.load_config(root)
    _write_plan(root, cfg, "my-thing", [
        _entry("T-01", POOL, progress="in_progress"), _entry("T-02", POOL),
        _entry("T-03", "nopool"), _entry("T-04", "gpu:3090"),
    ], pools=[("gpu:3090", 2)])

    def refuse(*args, **kwargs):
        pytest.fail("a plan in a checkout asked after a daemon's pools")

    monkeypatch.setattr(planneeds, "daemon_pools", refuse)
    return root, cfg, "my-thing"


def test_a_local_task_list_is_byte_identical_to_the_plans_own_count(local_plan):
    root, cfg, slug = local_plan

    result = runner.invoke(app, ["task", "list", "--json"])

    assert result.exit_code == 0, result.output
    listing = json.loads(result.stdout)
    # The pools map as it has always been written: a size and the holders.
    assert json.dumps(listing["pools"]) == json.dumps({
        "gpu:3090": {"size": 2, "holders": []},
        POOL: {"size": 1, "holders": ["T-01"]},
        "nopool": {"size": 1, "holders": []},
    })
    assert _ready(listing) == {"T-03", "T-04"}
    # And the whole output is what the plan module answers with no daemon named.
    found = plan.frontier(root, cfg, slug)
    ready = {t["id"] for t in found["tasks"] if t["ready"]}
    progress = plan.plan_progress(root, cfg, slug)
    nexts = set(progress["next_actionable"])
    assert result.stdout == json.dumps({
        "tasks": [
            {"id": t.id, "text": t.text, "progress": t.progress, "status": t.status,
             "implements": t.implements, "depends_on": t.depends_on, "next": t.id in nexts,
             "files": t.file_list, "needs": t.needs, "ready": t.id in ready}
            for t in plan.list_tasks(root, cfg, slug)
        ],
        "progress": progress,
        "pools": found["pools"],
    }) + "\n"
    assert found == plan.frontier(root, cfg, slug, daemon_pools=None)
    assert progress == plan.plan_progress(root, cfg, slug, daemon_pools=None)


def test_a_local_validation_is_byte_identical_and_knows_no_daemon(local_plan):
    root, cfg, slug = local_plan
    issues = plan.validate_plan(root, cfg, slug)

    result = runner.invoke(app, ["validate", "plan", "--json"])

    assert issues == plan.validate_plan(root, cfg, slug, daemon_pools=None)
    assert not any("nopool" in issue for issue in issues)
    assert json.loads(result.stdout)["issues"] == issues
    assert "nopool" not in result.output


def test_reading_a_local_plan_loads_no_pool_code(local_plan):
    root, _, _ = local_plan
    done = _fresh(
        """
        import sys
        from specflo.cli import app
        for verb in (["task", "list", "--json"], ["validate", "plan"]):
            try:
                app(verb)
            except SystemExit:
                pass
        print("pool:", " ".join(sorted(n for n in sys.modules if n.startswith("specflo.pool"))))
        """,
        cwd=root,
    )

    assert done.returncode == 0, done.stderr
    assert "pool: specflo.pool specflo.pool.cli_admin\n" in done.stdout
