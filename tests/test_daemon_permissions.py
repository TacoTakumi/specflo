"""The role-to-permissions table: who may do what on the daemon, in one place.

A role is one row of the table and holds a set of named permissions; every
access check asks the table, never the role's name. The developer holds every
permission the requester does and the developer's own besides: managing the
pool directory, releasing a lease from the pool page, attaching a console and
reading the bench's run files. The agent holds the gate relay and none of the
developer's. For each of the four surfaces a requester is refused and a
developer let through, on a bare daemon root: what a developer meets past the
check (no pool directory, the pool off, a missing field, a missing file) is
not the refusal.

No module of the daemon or the service compares a value with the literal
"developer" outside the table, so a check that bypasses it is found here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import auth, permissions, pool_web, store, web
from specflo.daemon.app import create_app
from specflo.daemon.permissions import (
    BENCH_RUNS, CONSOLE, EGRESS_SET, GATE_RELAY, PERMISSIONS, POOL_MANAGE, POOL_RELEASE, PRODUCT_MANAGE,
    ROLE_PERMISSIONS, can,
)
from specflo.service.pool_remote import CONSOLE_ATTACH_PATH, CONSOLE_DETACH_PATH, RELOAD_PATH

from conftest import role_token, web_sign_in

SRC = Path(__file__).resolve().parent.parent / "src" / "specflo"
DEVELOPER_ONLY = (POOL_MANAGE, POOL_RELEASE, CONSOLE, BENCH_RUNS, EGRESS_SET, PRODUCT_MANAGE)


# --- the table ------------------------------------------------------------------


def test_every_role_the_daemon_knows_has_a_row():
    assert set(ROLE_PERMISSIONS) == set(store.ROLES) | {"agent"}
    assert set(ROLE_PERMISSIONS) == set(auth.IDENTITIES)


def test_every_permission_a_row_holds_is_a_named_one():
    for held in ROLE_PERMISSIONS.values():
        assert held <= PERMISSIONS


def test_the_developer_holds_every_permission_the_requester_holds():
    assert ROLE_PERMISSIONS["requester"] <= ROLE_PERMISSIONS["developer"]


@pytest.mark.parametrize("permission", DEVELOPER_ONLY)
def test_the_developer_alone_holds_the_developer_permissions(permission):
    assert can("developer", permission)
    assert not can("requester", permission)
    assert not can("agent", permission)


def test_the_agent_alone_holds_the_gate_relay():
    assert can("agent", GATE_RELAY)
    assert not can("developer", GATE_RELAY)
    assert not can("requester", GATE_RELAY)


def test_an_unknown_role_or_permission_is_refused():
    assert not can("admin", POOL_MANAGE)
    assert not can("", POOL_MANAGE)
    assert not can("developer", "pool.everything")


def test_who_can_names_the_roles_that_hold_a_permission():
    assert permissions.who_can(POOL_MANAGE) == "the developer role"
    assert permissions.who_can(GATE_RELAY) == "the agent role"


# --- the four surfaces ------------------------------------------------------------


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def application(root):
    return create_app(root)


def browser(application, role: str) -> TestClient:
    client = TestClient(application, follow_redirects=False)
    web_sign_in(client, application.state.root, role)
    return client


def bearer(application, role: str) -> TestClient:
    client = TestClient(application)
    client.headers["Authorization"] = f"Bearer {role_token(application.state.root, role)}"
    return client


MANAGED = [
    web.DEFINITIONS_PATH, web.NEW_DEFINITION_PATH, web.TEAMS_PATH, web.NEW_TEAM_PATH,
]


@pytest.mark.parametrize("path", MANAGED)
def test_the_pool_management_pages_refuse_a_requester(application, path):
    response = browser(application, "requester").get(path)

    assert response.status_code == 403
    assert "developer role only" in response.text


@pytest.mark.parametrize("path", MANAGED)
def test_the_pool_management_pages_let_a_developer_through(application, path):
    response = browser(application, "developer").get(path)

    assert response.status_code != 403
    assert "role only" not in response.text


def _release(client: TestClient):
    return client.post(
        pool_web.release_url("lease-1"), data={"session": client.cookies[web.SESSION_COOKIE]}
    )


def test_the_pool_release_refuses_a_requester(application):
    response = _release(browser(application, "requester"))

    assert response.status_code == 403
    assert "developer role only" in response.text


def test_the_pool_release_lets_a_developer_through(application):
    # past the check the pool is off: there is no lease to release
    response = _release(browser(application, "developer"))

    assert response.status_code == 409
    assert "pool is off" in response.text


def test_the_pool_page_shows_the_release_control_by_the_table():
    assert pool_web.PoolView.releases.fget(_viewing("developer"))
    assert not pool_web.PoolView.releases.fget(_viewing("requester"))
    assert not pool_web.PoolView.releases.fget(_viewing("agent"))


def _viewing(role: str):
    class Viewer:
        viewer = role
    return Viewer()


@pytest.fixture
def pool_on(application):
    """A pool that stands, as far as the console routes look before the check:
    the routes take it, and a refusal or a missing field ends them before it is used."""
    application.state.pool = object()
    return application


@pytest.mark.parametrize("path", [CONSOLE_ATTACH_PATH, CONSOLE_DETACH_PATH])
@pytest.mark.parametrize("role", ["requester", "agent"])
def test_the_console_refuses_any_role_but_the_developer(pool_on, path, role):
    response = bearer(pool_on, role).post(path, json={})

    assert response.status_code == 403
    assert "developer role only" in response.json()["detail"]


@pytest.mark.parametrize("path", [CONSOLE_ATTACH_PATH, CONSOLE_DETACH_PATH])
def test_the_console_lets_a_developer_through(pool_on, path):
    # past the check the body names no slot
    response = bearer(pool_on, "developer").post(path, json={})

    assert response.status_code not in (401, 403)
    assert "slot" in response.json()["detail"]


def test_the_pool_reload_refuses_a_requester(application):
    response = bearer(application, "requester").post(RELOAD_PATH, json={})

    assert response.status_code == 403
    assert "developer role only" in response.json()["detail"]


def test_the_pool_reload_lets_a_developer_through(application):
    # past the check there is no pool directory to put in force
    response = bearer(application, "developer").post(RELOAD_PATH, json={})

    assert response.status_code not in (401, 403)
    assert "role only" not in response.json()["detail"]


RUN_FILE = web.BENCH_PATH + "/runs/run-1/record.json"


def test_the_bench_run_files_refuse_a_requester(application):
    response = browser(application, "requester").get(RUN_FILE)

    assert response.status_code == 403
    assert "developer role" in response.text


def test_the_bench_run_files_let_a_developer_through(application, root):
    record = root / "bench" / "runs" / "run-1" / "record.json"
    record.parent.mkdir(parents=True)
    record.write_text('{"run": 1}')

    response = browser(application, "developer").get(RUN_FILE)

    assert response.status_code == 200
    assert response.json() == {"run": 1}


def test_the_bench_menu_lists_the_runs_by_the_table(application, root):
    (root / "bench" / "runs" / "run-1").mkdir(parents=True)
    (root / "bench" / "runs" / "run-1" / "record.json").write_text("{}")

    assert "run-1" in browser(application, "developer").get(web.BENCH_PATH).text
    assert "run-1" not in browser(application, "requester").get(web.BENCH_PATH).text


# --- no check outside the table -------------------------------------------------------


def _modules() -> list[Path]:
    found = sorted((SRC / "daemon").glob("*.py")) + sorted((SRC / "service").glob("*.py"))
    return [path for path in found if path.name != "permissions.py"]


def _is_developer(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "developer"


def _developer_names(trees: dict[Path, ast.Module]) -> set[str]:
    """The names any of the modules binds to the literal "developer" by a plain
    assignment: a constant like that compared is the literal compared."""
    names = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _is_developer(node.value):
                names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
            elif isinstance(node, ast.AnnAssign) and node.value is not None and _is_developer(
                node.value
            ) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _names_developer(node: ast.AST, names: set[str]) -> bool:
    if _is_developer(node):
        return True
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        return node.attr in names
    return False


def _compares_with_developer(node: ast.Compare, names: set[str]) -> bool:
    operands = [node.left, *node.comparators]
    for op, (left, right) in zip(node.ops, zip(operands, operands[1:])):
        if isinstance(op, (ast.Eq, ast.NotEq)) and (
            _names_developer(left, names) or _names_developer(right, names)
        ):
            return True
        if isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, (ast.Tuple, ast.List, ast.Set)):
            if any(_names_developer(item, names) for item in right.elts):
                return True
    return False


def test_no_module_compares_with_the_developer_literal_outside_the_table():
    trees = {path: ast.parse(path.read_text()) for path in _modules()}
    names = _developer_names(trees)
    found = [
        f"{path.relative_to(SRC)}:{node.lineno}: {ast.unparse(node)}"
        for path, tree in trees.items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare) and _compares_with_developer(node, names)
    ]

    assert found == []


def test_the_scan_finds_the_comparisons_it_looks_for():
    source = (
        'READER = "developer"\n'
        'a = role == "developer"\n'
        'b = "developer" != role\n'
        'c = role in ("requester", "developer")\n'
        'd = role == READER\n'
        'e = role == module.READER\n'
        'f = role in ROLES\n'
        'g = {"developer": 1}\n'
    )
    tree = ast.parse(source)
    names = _developer_names({Path("probe.py"): tree})
    hits = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Compare) and _compares_with_developer(node, names)
    ]

    assert sorted(hits) == [2, 3, 4, 5, 6]
