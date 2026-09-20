"""The management pages for teams, for the developer's browser only.

A team is one markdown file in the pool directory's teams folder, and an
admin may write it by hand: the roles in the front matter, each a name, a
pool and a count, and notes in the body. The pages list the files, and create,
edit and delete one, as the pages for agent definitions do. A save writes the
text a hand would: the serialiser and the team check are each other's inverse.
The pages read the files on every request, so a hand edit shows on the next.

Before a file changes, the whole pool directory is checked with the candidate
in place, by the same check ``serve pool validate`` runs. A team with a role
on a pool that is not declared, or with a count above what its pool grants at
once, is refused with the fault beside the form, naming the team and the role,
and no file is written. A save that passed is followed by the reload of the
running pool and leaves an audit entry with the acting identity.

Every post runs the session-secret guard first. The requester's session gets
neither the pages nor the posts. No route of the web UI edits an account, a
member or a named pool, and none runs a member of the pool.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import replace
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from specflo.cli import app as cli
from specflo.daemon import pool_manage, pool_web, web
from specflo.daemon.app import create_app
from specflo.daemon.poolstore import WaitingRequest
from specflo.pool import cli_admin, teams
from specflo.pool import config as pool_config
from specflo.pool.service import UnknownTeam
from specflo.service import pool_remote

# The pool tests' rig and helpers and those of the definition pages, imported
# rather than copied. The rig's fixtures are named here so that pytest finds
# them from this module.
from pool.conftest import no_real_llama_swap, pool_rig  # noqa: F401  (fixtures)
from pool.test_lease_request import audit_records
from pool.test_reload import PAIR, Served, write_workers
from pool.test_team_lease import REVIEW, active, write_team_pool
from test_web_pool import signed_in
from test_web_pool_definitions import NOT_THIS_SESSION, post, root, runner, shown  # noqa: F401
from test_web_pool_definitions import snapshot

LIST = "/pool/teams"
NEW = "/pool/teams/new"

# What the form posts for a team: one role to a line, a name, a pool and a count.
FIELDS = {
    "name": "build",
    "roles": "designer workers 1\r\nbuilder  workers 1\r\n\r\nreviewer critics 1\r\n",
    "notes": "The orchestrator leads.\r\n\r\nMembers do not message each other.\r\n",
}
BUILD = teams.Team(
    name="build",
    roles=(
        teams.Role(name="designer", pool="workers", count=1),
        teams.Role(name="builder", pool="workers", count=1),
        teams.Role(name="reviewer", pool="critics", count=1),
    ),
    notes="The orchestrator leads.\n\nMembers do not message each other.",
)
BUILD_TEXT = """\
---
roles:
  - {name: designer, pool: workers, count: 1}
  - {name: builder, pool: workers, count: 1}
  - {name: reviewer, pool: critics, count: 1}
---

The orchestrator leads.

Members do not message each other.
"""


def edit_url(name: str) -> str:
    return f"{LIST}/{name}/edit"


def delete_url(name: str) -> str:
    return f"{LIST}/{name}/delete"


def folder(root: Path) -> Path:
    return cli_admin.pool_dir(root) / teams.TEAMS_DIR


def listed(root: Path) -> list[str]:
    """Every entry of the teams folder, a file a save left behind among them."""
    return sorted(entry.name for entry in folder(root).iterdir())


def standing(root: Path) -> dict[str, pool_config.Pool]:
    config, faults = pool_config.load_pool_config(cli_admin.pool_dir(root))
    assert faults == []
    return {pool.name: pool for pool in config.pools}


def saves(root: Path) -> list[dict]:
    return [r for r in audit_records(root) if r["operation"].startswith("team_")]


def beside_roles(response) -> str:
    found = re.findall(r'<p class="error" data-field="roles">(.*?)</p>', response.text, re.S)
    assert found, response.text
    return " ".join(unescape(" ".join(found)).split())


@pytest.fixture
def team_root(pool_rig) -> Path:  # noqa: F811  (the fixture of the same name)
    """A daemon root whose pool directory declares the pool "workers" of two
    members and "critics" of one, and the team "review"."""
    write_team_pool(pool_rig)
    return pool_rig.root


# -- the text a save writes ------------------------------------------------------


def test_a_team_is_written_as_a_hand_would_write_it_and_comes_back_as_it_was(team_root):
    path = folder(team_root) / "build.md"

    written = teams.serialise_team(BUILD)

    assert written == BUILD_TEXT
    assert teams.check_team(written, path, standing(team_root)) == (BUILD, [])


def test_a_team_with_no_notes_has_no_body_and_awkward_values_come_back(team_root):
    awkward = teams.Team(
        name="odd",
        roles=(
            teams.Role(name="yes", pool="workers", count=2),
            teams.Role(name="lead: the first, # one", pool="critics", count=1),
        ),
        notes="First part.\n\n---\n\nSecond part: after a rule.",
    )
    bare = replace(BUILD, notes="")
    path = folder(team_root) / "odd.md"

    assert teams.check_team(teams.serialise_team(awkward), path, standing(team_root)) == (
        awkward, []
    )
    assert teams.serialise_team(bare) == BUILD_TEXT.split("---\n\n")[0] + "---\n"
    assert teams.check_team(
        teams.serialise_team(bare), folder(team_root) / "build.md", standing(team_root)
    ) == (bare, [])


# -- the list ----------------------------------------------------------------------


def test_the_list_shows_each_team_file_with_its_roles(team_root):
    (folder(team_root) / "build.md").write_text(BUILD_TEXT, encoding="utf-8")
    client = signed_in(create_app(team_root))

    response = client.get(LIST)

    assert response.status_code == 200, response.text
    rows = dict(re.findall(r'<tr data-team="([^"]+)">(.*?)</tr>', response.text, re.S))
    assert list(rows) == ["build", "review"]
    assert "designer workers 1" in " ".join(rows["build"].split())
    assert "reviewer critics 1" in " ".join(rows["build"].split())
    assert "critic critics 1" in " ".join(rows["review"].split())
    assert "designer" not in rows["review"]
    assert f'href="{edit_url("review")}"' in rows["review"]
    assert f'href="{NEW}"' in response.text
    assert f'href="{web.DEFINITIONS_PATH}"' in response.text


def test_a_hand_edit_shows_on_the_next_read_of_the_list_and_of_the_form(team_root):
    client = signed_in(create_app(team_root))
    assert "worker workers 1" in shown(client.get(LIST))
    path = folder(team_root) / "review.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("count: 1\n  name: worker", "count: 2\n  name: worker")
        .replace("Review each change.", "Review each change, edited by hand."),
        encoding="utf-8",
    )

    assert "worker workers 2" in shown(client.get(LIST))
    form = client.get(edit_url("review"))
    assert form.status_code == 200, form.text
    assert "worker workers 2\ncritic critics 1" in unescape(form.text)
    assert "Review each change, edited by hand." in unescape(form.text)


def test_a_file_that_does_not_load_is_listed_with_its_fault_and_has_no_form(team_root):
    (folder(team_root) / "review.md").write_text("no front matter\n", encoding="utf-8")
    client = signed_in(create_app(team_root))

    response = client.get(LIST)

    assert response.status_code == 200
    assert "the file must open with a '---' block" in shown(response)
    assert f'href="{edit_url("review")}"' not in response.text
    assert client.get(edit_url("review")).status_code == 409
    assert client.get(edit_url("nobody")).status_code == 404


def test_a_team_that_a_count_keeps_from_standing_has_a_form_and_a_save_mends_it(team_root):
    path = folder(team_root) / "review.md"
    path.write_text(teams.serialise_team(replace(
        REVIEW, roles=(REVIEW.roles[0], replace(REVIEW.roles[1], count=4)),
def test_a_file_that_is_not_utf8_is_listed_with_its_fault_and_has_no_form(team_root):
    (folder(team_root) / "review.md").write_bytes(b"---\nentry: caf\xe9\n---\n")
    client = signed_in(create_app(team_root))

    response = client.get(LIST)

    assert response.status_code == 200
    assert "not UTF-8 text" in shown(response)
    assert f'href="{edit_url("review")}"' not in response.text
    assert client.get(edit_url("review")).status_code == 409


    )), encoding="utf-8")
    app = create_app(team_root)
    assert app.state.pool is None
    client = signed_in(app)

    response = client.get(LIST)
    form = client.get(edit_url("review"))

    assert "team 'review' role 'critic'" in shown(response)
    assert form.status_code == 200, form.text
    assert "critic critics 4" in form.text
    saved = post(client, edit_url("review"), roles="worker workers 1\ncritic critics 1")
    assert saved.status_code == 303, saved.text
    assert [team.name for team in app.state.pool.config.teams] == ["review"]


# -- create ------------------------------------------------------------------------


def test_a_created_team_is_a_file_the_validator_accepts_and_the_running_pool_holds(team_root):
    app = create_app(team_root)
    service = app.state.pool
    client = signed_in(app)
    assert client.get(NEW).status_code == 200

    response = post(client, LIST, **FIELDS)

    assert response.status_code == 303, response.text
    assert response.headers["location"] == LIST
    path = folder(team_root) / "build.md"
    assert path.read_text(encoding="utf-8") == BUILD_TEXT
    checked = runner.invoke(cli, ["serve", "--root", str(team_root), "pool", "validate"])
    assert checked.exit_code == 0, checked.output
    assert "2 teams" in checked.output
    # the running service, in the same process, holds what the files say now
    assert app.state.pool is service
    assert service.config.teams == (BUILD, replace(REVIEW, notes="Review each change."))
    assert [(r["identity"], r["operation"], r["id"]) for r in audit_records(team_root)] == [
        ("developer", "team_save", "build"),
        ("developer", "pool_reload", None),
    ]
    assert "build" in client.get(LIST).text
    # the check's copy and the step the file was replaced in have left nothing
    assert listed(team_root) == ["build.md", "review.md"]
    assert not list(team_root.glob(pool_manage.CANDIDATE_PREFIX + "*"))


def test_the_first_team_of_a_pool_directory_makes_the_teams_folder(team_root):
    shutil.rmtree(folder(team_root))
    client = signed_in(create_app(team_root))

    response = post(client, LIST, **FIELDS)

    assert response.status_code == 303, response.text
    assert listed(team_root) == ["build.md"]


def test_a_refused_first_team_leaves_no_teams_folder(root):  # noqa: F811
    assert not folder(root).exists()
    client = signed_in(create_app(root))

    # the pool file of ``serve pool init`` declares no pool, so no role can stand
    response = post(client, LIST, **FIELDS)

    assert response.status_code == 400
    assert "'workers' is not a declared pool." in beside_roles(response)
    assert not folder(root).exists()
    assert saves(root) == []


@pytest.mark.parametrize(
    ("roles", "fault"),
    [
        (
            "designer workers 1\nreviewer reviewers 1\n",
            "team 'build' role 'reviewer': pool: 'reviewers' is not a declared pool.",
        ),
        (
            "designer workers 1\nreviewer critics 2\n",
            "team 'build' role 'reviewer': count: 2 is more than pool 'critics' grants at "
            "once, 1.",
        ),
    ],
    ids=["a missing pool", "a count above the pool's size"],
)
def test_a_team_with_a_role_that_can_never_be_granted_is_refused_and_no_file_is_written(
    team_root, roles, fault
):
    app = create_app(team_root)
    client = signed_in(app)
    before = snapshot(team_root)

    response = post(client, LIST, **{**FIELDS, "roles": roles})

    assert response.status_code == 400
    assert fault in beside_roles(response)
    # the form is still there with what was posted
    assert f'action="{LIST}"' in response.text
    assert 'value="build"' in response.text
    assert roles in unescape(response.text)
    assert "Members do not message each other." in response.text
    assert listed(team_root) == ["review.md"]
    assert snapshot(team_root) == before
    assert [team.name for team in app.state.pool.config.teams] == ["review"]
    assert audit_records(team_root) == []


@pytest.mark.parametrize(
    "roles", ["designer workers\n", "designer workers one\n", "designer\n", "designer workers 1.5"]
)
def test_a_line_that_is_not_a_role_is_refused_beside_the_field(team_root, roles):
    client = signed_in(create_app(team_root))
    before = snapshot(team_root)

    response = post(client, LIST, **{**FIELDS, "roles": "builder workers 1\n" + roles})

    assert response.status_code == 400
    assert "line 2" in beside_roles(response)
    assert "a name, a pool and a count" in beside_roles(response)
    assert snapshot(team_root) == before
    assert saves(team_root) == []


def test_a_team_with_no_role_is_refused_by_the_validator(team_root):
    client = signed_in(create_app(team_root))
    before = snapshot(team_root)

    response = post(client, LIST, **{**FIELDS, "roles": " \r\n"})

    assert response.status_code == 400
    assert "a list of at least one role" in beside_roles(response)
    assert snapshot(team_root) == before


def test_a_create_over_a_file_that_is_there_is_refused(team_root):
    client = signed_in(create_app(team_root))
    before = snapshot(team_root)

    response = post(client, LIST, **{**FIELDS, "name": "review"})

    assert response.status_code == 400
    assert "there is a team 'review' already" in shown(response)
    assert "designer workers 1" in response.text
    assert snapshot(team_root) == before
    assert saves(team_root) == []


@pytest.mark.parametrize("name", ["", "../build", "a/b", ".build", "..", "bu ild", "build.md"])
def test_a_name_that_is_not_a_plain_name_is_refused_and_nothing_is_written(
    team_root, tmp_path, name
):
    client = signed_in(create_app(team_root))
    before = snapshot(team_root)
    outside = sorted(p.name for p in tmp_path.iterdir())

    response = post(client, LIST, **{**FIELDS, "name": name})

    assert response.status_code == 400
    assert re.search(r'<p class="error" data-field="name">', response.text), response.text
    assert snapshot(team_root) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == outside


# -- save --------------------------------------------------------------------------


def test_a_save_writes_the_file_reloads_the_running_pool_and_is_audited(team_root):
    app = create_app(team_root)
    service = app.state.pool
    client = signed_in(app)

    # the name a form posts renames nothing: the path names the team
    response = post(client, edit_url("review"), **FIELDS)

    assert response.status_code == 303, response.text
    assert response.headers["location"] == LIST
    saved = replace(BUILD, name="review")
    path = folder(team_root) / "review.md"
    assert path.read_text(encoding="utf-8") == BUILD_TEXT
    assert teams.check_team(BUILD_TEXT, path, standing(team_root)) == (saved, [])
    assert listed(team_root) == ["review.md"]
    assert app.state.pool is service
    assert service.config.teams == (saved,)
    assert [(r["identity"], r["operation"], r["id"]) for r in audit_records(team_root)] == [
        ("developer", "team_save", "review"),
        ("developer", "pool_reload", None),
    ]


def test_a_refused_save_leaves_the_bytes_of_the_file_as_they_were(team_root):
    app = create_app(team_root)
    client = signed_in(app)
    path = folder(team_root) / "review.md"
    before = path.read_bytes()
    everything = snapshot(team_root)

    response = post(client, edit_url("review"), **{**FIELDS, "roles": "critic critics 3"})

    assert response.status_code == 400
    assert "team 'review' role 'critic': count: 3 is more than pool 'critics'" in beside_roles(
        response
    )
    assert f'action="{edit_url("review")}"' in response.text
    assert "critic critics 3" in response.text
    assert path.read_bytes() == before
    assert snapshot(team_root) == everything
    assert app.state.pool.config.teams[0].roles == REVIEW.roles
    assert audit_records(team_root) == []


def test_a_save_into_a_directory_that_has_another_fault_is_refused_with_every_fault(team_root):
    definition = cli_admin.pool_dir(team_root) / pool_config.DEFINITIONS_DIR / "rebaser.md"
    definition.write_text("no front matter\n", encoding="utf-8")
    client = signed_in(create_app(team_root))
    before = snapshot(team_root)

    response = post(client, LIST, **FIELDS)

    assert response.status_code == 400
    assert "definition 'rebaser'" in shown(response)
    assert str(definition) in shown(response)
    assert snapshot(team_root) == before
    assert saves(team_root) == []


# -- delete ------------------------------------------------------------------------


def test_deleting_a_team_removes_the_file_and_reloads(team_root):
    app = create_app(team_root)
    client = signed_in(app)
    assert [team.name for team in app.state.pool.config.teams] == ["review"]

    response = post(client, delete_url("review"))

    assert response.status_code == 303, response.text
    assert response.headers["location"] == LIST
    assert listed(team_root) == []
    assert app.state.pool.config.teams == ()
    assert [(r["identity"], r["operation"], r["id"]) for r in audit_records(team_root)] == [
        ("developer", "team_delete", "review"),
        ("developer", "pool_reload", None),
    ]
    assert post(client, delete_url("review")).status_code == 404
    assert "No team file is in the pool directory." in shown(client.get(LIST))


def test_a_team_lease_and_a_waiting_request_of_a_deleted_team_end_as_after_a_hand_edit(pool_rig):
    write_workers(pool_rig, teams=PAIR)
    served = Served(pool_rig)
    team = served.granted(team="pair")
    with pool_rig.store() as store:
        store.add_waiting(WaitingRequest(
            id="w-1", pool=None, team="pair", holder_label="someone",
            arrived="2026-03-01T11:00:00.000+00:00",
        ))

    response = post(signed_in(served.application), delete_url("pair"))

    # the delete is made with the lease out: the members of a team are those
    # of its pools, which stand, so nothing is in the way of the reload
    assert response.status_code == 303, response.text
    assert served.state.pool_errors == ()
    assert served.state.pool.config.teams == ()
    assert len(active(pool_rig)) == 2
    # the request that waits is told so at its next look
    with pytest.raises(UnknownTeam):
        served.state.pool.grant_team(
            "pair", holder_label="someone", cwd=pool_rig.work, waiting_id="w-1"
        )
    # and the team lease is given back as one
    ended = served.release(team["team_lease_id"], team["members"][0]["token"])
    assert ended.status_code == 200, ended.text
    assert active(pool_rig) == []


# -- who may, and how ----------------------------------------------------------------


def test_the_requesters_session_gets_no_management_page(team_root):
    client = signed_in(create_app(team_root), "requester")

    for url in (LIST, NEW, edit_url("review")):
        response = client.get(url)
        assert response.status_code == 403, url
        assert "developer identity only" in shown(response)
        assert "critics" not in response.text


def test_the_requesters_posts_are_refused_and_change_nothing(team_root):
    client = signed_in(create_app(team_root), "requester")
    before = snapshot(team_root)

    for url in (LIST, edit_url("review"), delete_url("review")):
        response = post(client, url, **FIELDS)
        assert response.status_code == 403, url
        assert "developer identity only" in shown(response)
    assert snapshot(team_root) == before
    assert audit_records(team_root) == []


@pytest.mark.parametrize("identity", ["developer", "requester"])
def test_a_post_without_the_session_secret_is_refused_before_anything_else(team_root, identity):
    client = signed_in(create_app(team_root), identity)
    before = snapshot(team_root)

    for url in (LIST, edit_url("review"), delete_url("review")):
        for secret in ({}, {"session": "not-the-secret"}):
            response = client.post(url, data={**FIELDS, **secret})
            assert response.status_code == 403, url
            assert NOT_THIS_SESSION in shown(response)
    assert snapshot(team_root) == before
    assert audit_records(team_root) == []


def test_a_browser_with_no_session_is_sent_to_sign_in(team_root):
    client = TestClient(create_app(team_root), follow_redirects=False)

    for response in (client.get(LIST), client.get(NEW), client.post(LIST, data=FIELDS)):
        assert response.status_code == 303
        assert response.headers["location"] == web.SIGNIN_PATH
    assert listed(team_root) == ["review.md"]


def test_a_root_with_no_pool_directory_has_nothing_to_manage(tmp_path):
    client = signed_in(create_app(tmp_path / "daemon"))

    assert "No pool is configured on this daemon." in shown(client.get(LIST))
    response = post(client, LIST, **FIELDS)
    assert response.status_code == 409
    assert not cli_admin.pool_dir(tmp_path / "daemon").exists()


# -- the routes ----------------------------------------------------------------------


def test_the_team_routes_are_those_of_teams_and_nothing_else():
    routes = sorted(
        (route.path, method)
        for route in pool_manage.team_pages.routes for method in route.methods
    )

    assert routes == [
        (LIST, "GET"),
        (LIST, "POST"),
        (NEW, "GET"),
        (LIST + "/{name}/delete", "POST"),
        (LIST + "/{name}/edit", "GET"),
        (LIST + "/{name}/edit", "POST"),
    ]
    assert web.TEAMS_PATH == LIST


def test_every_form_posts_to_a_url_helper_and_the_lists_link_to_each_other():
    names = ("pool_teams.html", "pool_team_form.html", "pool_definitions.html", "pool.html")
    templates = {
        name: (web.TEMPLATES_DIR / name).read_text(encoding="utf-8") for name in names
    }

    actions = {
        name: re.findall(r'<form[^>]*action="([^"]*)"', templates[name]) for name in names[:2]
    }
    assert actions == {
        "pool_teams.html": ["{{ team_delete_url(row.name) }}"],
        "pool_team_form.html": ["{{ team_save_url(name) }}"],
    }
    assert 'href="{{ pool_path }}"' in templates["pool_teams.html"]
    assert 'href="{{ definitions_path }}"' in templates["pool_teams.html"]
    assert 'href="{{ teams_path }}"' in templates["pool_definitions.html"]
    # the pool page links the list for the developer alone
    manage = re.search(
        r'{% if identity == "developer" %}(.*?){% endif %}', templates["pool.html"], re.S
    ).group(1)
    assert 'href="{{ teams_path }}"' in manage
    assert templates["pool.html"].count("teams_path") == 1


# Verbs by which a route changes something.
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Every route by which a browser changes something, and nothing else.
BROWSER_POSTS = {
    web.SIGNIN_PATH,
    web.START_PROJECT_PATH,
    web.TAKE_PATH,
    web.CHAT_PATH,
    # The older control of a project's own agent, which the project page has.
    # It starts no member of the pool: a member starts with a lease, never
    # from a page.
    web.START_AGENT_PATH,
    pool_web.RELEASE_PATH,
    web.DEFINITIONS_PATH,
    web.EDIT_DEFINITION_PATH,
    web.DELETE_DEFINITION_PATH,
    web.TEAMS_PATH,
    web.EDIT_TEAM_PATH,
    web.DELETE_TEAM_PATH,
}
# Every route of the pool's own API that changes something: leases, consoles
# and the reload of the files. None writes a file of the pool directory.
POOL_API_POSTS = {
    pool_remote.LEASES_PATH,
    pool_remote.HELD_PATH,
    pool_remote.LEASES_PATH + "/{lease_id}/release",
    pool_remote.CONSOLE_ATTACH_PATH,
    pool_remote.CONSOLE_DETACH_PATH,
    pool_remote.RELOAD_PATH,
}
# The operations of a project's plan that speak of a pool: a pool of a plan is
# a limit on how many of its tasks run at once, not a named pool of agents.
PLAN_POOLS = {"add_pool", "list_pools"}
# What a route that edits an account or a member would be called.
NOT_EDITED = ("account", "member")
# What a route of the pool that runs a member, or edits the pool file, would be called.
NOT_DONE = (*NOT_EDITED, "prompt", "run", "start", "spawn", "launch", "exec", "yaml", "config")
BROWSER_MODULES = {web.__name__, pool_web.__name__, pool_manage.__name__}


def _walk(routes):
    """Every route, descending into a router the application holds as one entry."""
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _walk(nested.routes)
        elif hasattr(route, "methods"):
            yield route


def _posts(routes) -> set[str]:
    return {route.path for route in routes if (route.methods or set()) & MUTATING}


def test_no_route_edits_an_account_a_member_or_a_named_pool_and_none_runs_a_member(tmp_path):
    routers = (web.front_door, web.pages, pool_web.pages, pool_manage.pages,
               pool_manage.team_pages)
    table = list(_walk(create_app(tmp_path / "daemon").routes))
    browser = [route for route in table if route.endpoint.__module__ in BROWSER_MODULES]

    # the walk reaches the routes, those of every router of the web UI among them
    assert web.TEAMS_PATH in {route.path for route in table}
    assert {route.path for router in routers for route in router.routes} == {
        route.path for route in browser
    }
    # a browser changes something by the listed routes and by no other
    assert _posts(browser) == BROWSER_POSTS
    assert _posts(route for router in routers for route in router.routes) == BROWSER_POSTS
    # the pool changes by those and by its API; nothing else touches it
    pool = [
        route for route in table
        if route.path.startswith((web.POOL_PATH, pool_remote.POOL_PATH))
    ]
    in_browser = {path for path in BROWSER_POSTS if path.startswith(web.POOL_PATH)}
    assert _posts(pool) == in_browser | POOL_API_POSTS
    for route in pool:
        named = f"{route.path} {route.name}".lower()
        assert not [word for word in NOT_DONE if word in named], route.path
        # a route acts on one lease, one definition or one team at most
        assert set(route.param_convertors) <= {"lease_id", "name"}, route.path
    for route in table:
        named = f"{route.path} {route.name}".lower()
        assert not [word for word in NOT_EDITED if word in named], route.path
        if "pool" in named and route not in pool:
            assert route.name in PLAN_POOLS, route.path
