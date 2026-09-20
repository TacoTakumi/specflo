"""The management pages for agent definitions, for the developer's browser only.

An agent definition is one markdown file in the pool directory's definitions
folder, and an admin may write it by hand. The pages list the files, and
create, edit and delete one. A save writes the same text a hand would: the
serialiser and the parser are each other's inverse, and the shipped
definitions come back byte for byte. The pages read the files on every
request, so a hand edit shows on the next one.

Before a file changes, the whole pool directory is checked with the candidate
in place, by the same check ``serve pool validate`` runs. A candidate with a
fault is refused with the fault beside the form, the posted values kept, and
the file's bytes as they were. A save that passed is followed by the reload of
the running pool and leaves an audit entry with the acting identity.

A definition that a pool binds is not deleted: the refusal names the pool.

The definitions folder, or one file of it, may be a symlink to a place outside
the pool directory. The check of a candidate stays in its copy then too: what
it refuses leaves the real file as it was, and makes none.

Every post runs the session-secret guard first. The requester's session gets
neither the pages nor the posts.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import replace
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo.cli import app as cli
from specflo.daemon import pool_manage, pool_routes, web
from specflo.daemon.app import create_app
from specflo.pool import cli_admin, definitions
from specflo.pool import config as pool_config

# The pool tests' rig and helpers, imported rather than copied. The rig's
# fixtures are named here so that pytest finds them from this module.
from pool.conftest import no_real_llama_swap, pool_rig  # noqa: F401  (fixtures)
from pool.test_lease_request import REBASER, audit_records, write_pool
from test_web_pool import signed_in

runner = CliRunner()

LIST = "/pool/definitions"
NEW = "/pool/definitions/new"
EMPTY_BODY = "empty; the body is the role's system prompt."
NOT_THIS_SESSION = "That form did not come from this session."

# What the form posts for a definition, one list item to a line.
FIELDS = {
    "name": "porter",
    "role": "Carries a change from one branch to another",
    "tools": "read\nbash\n",
    "skills": "",
    "deny": "git push\ngit reset\n",
    "env": "",
    "credentials": "",
    "needs": "code\n",
    "egress": "local",
    "project_context": "on",
    "prompt": "You are the porter.\n\n- Carry one change at a time.\n",
}
PORTER = definitions.AgentDefinition(
    name="porter",
    role="Carries a change from one branch to another",
    prompt="You are the porter.\n\n- Carry one change at a time.",
    tools=("read", "bash"),
    deny=("git push", "git reset"),
    needs=("code",),
    egress="local",
    project_context=True,
)


def edit_url(name: str) -> str:
    return f"{LIST}/{name}/edit"


def delete_url(name: str) -> str:
    return f"{LIST}/{name}/delete"


@pytest.fixture
def root(tmp_path) -> Path:
    """A daemon root whose pool directory is what ``serve pool init`` writes:
    a pool file that declares nothing, and the shipped definitions."""
    root = tmp_path / "daemon"
    result = runner.invoke(cli, ["serve", "--root", str(root), "pool", "init"])
    assert result.exit_code == 0, result.output
    return root


def folder(root: Path) -> Path:
    return cli_admin.pool_dir(root) / pool_config.DEFINITIONS_DIR


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file under the daemon root's pool directory, and every entry of
    the root itself, so a save that was refused is seen to have left nothing."""
    directory = cli_admin.pool_dir(root)
    files = {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(directory.rglob("*")) if path.is_file()
    }
    entries = {name.name: b"" for name in root.iterdir() if name.name != "audit.jsonl"}
    return {**entries, **files}


def post(client, url: str, **fields) -> object:
    return client.post(url, data={"session": client.cookies[web.SESSION_COOKIE], **fields})


def shown(response) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", response.text)).split())


def saves(root: Path) -> list[dict]:
    return [r for r in audit_records(root) if r["operation"].startswith("definition_")]


# -- the text a save writes ------------------------------------------------------


SHIPPED = sorted(cli_admin.SHIPPED_DIR.glob("*.md"))


@pytest.mark.parametrize("shipped", SHIPPED, ids=lambda path: path.stem)
def test_a_shipped_definition_is_written_back_byte_for_byte(shipped):
    text = shipped.read_text(encoding="utf-8")
    definition = definitions.parse_definition(text, shipped)

    written = definitions.serialise_definition(definition)

    assert written == text
    assert definitions.parse_definition(written, shipped) == definition


def test_values_that_yaml_would_read_otherwise_come_back_as_they_were():
    awkward = replace(
        PORTER,
        role="Ports: a change # and says so",
        tools=("read", "yes", "1"),
        deny=("git commit, git push", "rm -rf: all", "[x]"),
        prompt="First part.\n\n---\n\nSecond part: after a rule.",
        egress=definitions.DEFAULT_EGRESS,
        project_context=False,
    )

    written = definitions.serialise_definition(awkward)

    assert definitions.parse_definition(written, Path("porter.md")) == awkward
    assert "project_context" not in written


# -- the list ----------------------------------------------------------------------


def test_the_list_shows_each_definition_file_with_its_role_and_the_pools_that_bind_it(pool_rig):
    write_pool(pool_rig)
    (folder(pool_rig.root) / "spare.md").write_text(
        REBASER.replace("Rebases the work branch", "Stands by"), encoding="utf-8"
    )
    client = signed_in(create_app(pool_rig.root))

    response = client.get(LIST)

    assert response.status_code == 200, response.text
    rows = dict(re.findall(r'<tr data-definition="([^"]+)">(.*?)</tr>', response.text, re.S))
    assert list(rows) == ["rebaser", "spare"]
    assert "Rebases the work branch and reports conflicts" in rows["rebaser"]
    assert "rebasers" in rows["rebaser"]
    assert "Stands by" in rows["spare"]
    assert "rebasers" not in rows["spare"]
    assert f'href="{edit_url("spare")}"' in rows["spare"]
    assert f'href="{NEW}"' in response.text


def test_a_hand_edit_shows_on_the_next_read_of_the_list_and_of_the_form(root):
    client = signed_in(create_app(root))
    assert "Does one task it is handed" in client.get(LIST).text
    path = folder(root) / "worker.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("Does one task it is handed", "Does what the hand edit says")
        .replace("You are a worker.", "You are a worker, edited by hand."),
        encoding="utf-8",
    )

    assert "Does what the hand edit says" in client.get(LIST).text
    form = client.get(edit_url("worker"))
    assert form.status_code == 200, form.text
    assert "You are a worker, edited by hand." in unescape(form.text)


def test_a_file_that_does_not_load_is_listed_with_its_fault_and_has_no_form(root):
    (folder(root) / "worker.md").write_text("no front matter\n", encoding="utf-8")
    client = signed_in(create_app(root))

    listed = client.get(LIST)

    assert listed.status_code == 200
    assert "the file must open with a '---' block" in shown(listed)
    assert f'href="{edit_url("worker")}"' not in listed.text
    assert client.get(edit_url("worker")).status_code == 409
    assert client.get(edit_url("nobody")).status_code == 404


# -- create ------------------------------------------------------------------------


def test_a_created_definition_is_a_file_the_validator_accepts(root):
    client = signed_in(create_app(root))
    assert client.get(NEW).status_code == 200

    response = post(client, LIST, **FIELDS)

    assert response.status_code == 303, response.text
    assert response.headers["location"] == LIST
    path = folder(root) / "porter.md"
    assert path.read_text(encoding="utf-8") == definitions.serialise_definition(PORTER)
    assert definitions.load_definition(path) == PORTER
    config, faults = pool_config.load_pool_config(cli_admin.pool_dir(root))
    assert faults == []
    assert PORTER in config.definitions
    checked = runner.invoke(cli, ["serve", "--root", str(root), "pool", "validate"])
    assert checked.exit_code == 0, checked.output
    assert "6 definitions" in checked.output
    assert "porter" in client.get(LIST).text
    # the check's copy and the step the file was replaced in have left nothing
    assert len(list(folder(root).iterdir())) == 6
    assert not list(root.glob(pool_manage.CANDIDATE_PREFIX + "*"))


def test_a_created_definition_shows_unchanged_after_a_hand_edit_of_another_field(root):
    client = signed_in(create_app(root))
    assert post(client, LIST, **FIELDS).status_code == 303
    path = folder(root) / "porter.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("egress: local", "egress: open"), encoding="utf-8"
    )

    form = client.get(edit_url("porter")).text

    assert re.search(r'<option value="open" selected>', form)
    assert PORTER.role in unescape(form)
    assert "Carry one change at a time." in unescape(form)


def test_a_create_over_a_file_that_is_there_is_refused(root):
    client = signed_in(create_app(root))
    before = snapshot(root)

    response = post(client, LIST, **{**FIELDS, "name": "worker"})

    assert response.status_code == 400
    assert "there is a definition 'worker' already" in shown(response)
    assert FIELDS["role"] in unescape(response.text)
    assert snapshot(root) == before
    assert saves(root) == []


@pytest.mark.parametrize("name", ["", "../porter", "a/b", ".porter", "..", "por ter", "porter.md"])
def test_a_name_that_is_not_a_plain_name_is_refused_and_nothing_is_written(root, tmp_path, name):
    client = signed_in(create_app(root))
    before = snapshot(root)
    outside = sorted(p.name for p in tmp_path.iterdir())

    response = post(client, LIST, **{**FIELDS, "name": name})

    assert response.status_code == 400
    assert re.search(r'<p class="error" data-field="name">', response.text), response.text
    assert snapshot(root) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == outside


# -- save --------------------------------------------------------------------------


def test_a_save_writes_the_file_reloads_the_running_pool_and_is_audited(pool_rig):
    write_pool(pool_rig)
    app = create_app(pool_rig.root)
    service = app.state.pool
    assert [d.prompt for d in service.config.definitions] == ["You are the rebaser."]
    client = signed_in(app)
    # the pool's one member has no label, so the definition may need none
    fields = {**FIELDS, "prompt": "You are the rebaser. Name every conflict.", "needs": ""}

    response = post(client, edit_url("rebaser"), **fields)

    assert response.status_code == 303, response.text
    assert response.headers["location"] == LIST
    path = folder(pool_rig.root) / "rebaser.md"
    saved = definitions.load_definition(path)
    assert saved == replace(PORTER, name="rebaser", prompt=fields["prompt"], needs=())
    assert path.read_text(encoding="utf-8") == definitions.serialise_definition(saved)
    # the running service, in the same process, holds what the file says now
    assert app.state.pool is service
    assert service.config.definitions == (saved,)
    records = audit_records(pool_rig.root)
    assert [(r["identity"], r["operation"], r["id"]) for r in records] == [
        ("developer", "definition_save", "rebaser"),
        ("developer", "pool_reload", None),
    ]


def test_the_name_a_form_posts_does_not_rename_a_definition(root):
    client = signed_in(create_app(root))

    response = post(client, edit_url("worker"), **{**FIELDS, "name": "critic"})

    assert response.status_code == 303, response.text
    assert definitions.load_definition(folder(root) / "worker.md") == replace(PORTER, name="worker")
    assert "You are a critic." in (folder(root) / "critic.md").read_text(encoding="utf-8")


def test_a_save_with_an_empty_prompt_is_refused_and_the_file_is_as_it_was(pool_rig):
    write_pool(pool_rig)
    app = create_app(pool_rig.root)
    client = signed_in(app)
    before = snapshot(pool_rig.root)

    response = post(client, edit_url("rebaser"), **{**FIELDS, "prompt": " \r\n "})

    assert response.status_code == 400
    error = re.search(r'<p class="error" data-field="body">(.*?)</p>', response.text, re.S)
    assert error, response.text
    assert EMPTY_BODY in unescape(error.group(1))
    # the form is still there with what was posted
    assert f'action="{edit_url("rebaser")}"' in response.text
    assert FIELDS["role"] in unescape(response.text)
    assert "git reset" in response.text
    assert snapshot(pool_rig.root) == before
    assert [d.prompt for d in app.state.pool.config.definitions] == ["You are the rebaser."]
    assert audit_records(pool_rig.root) == []


def test_a_create_with_an_empty_prompt_is_refused_and_no_file_is_made(root):
    client = signed_in(create_app(root))
    before = snapshot(root)

    response = post(client, LIST, **{**FIELDS, "prompt": ""})

    assert response.status_code == 400
    assert EMPTY_BODY in shown(response)
    assert 'value="porter"' in response.text
    assert snapshot(root) == before
    assert saves(root) == []


def test_a_save_that_a_pool_cannot_stand_with_is_refused_with_the_pools_fault(pool_rig):
    write_pool(pool_rig)
    client = signed_in(create_app(pool_rig.root))
    before = snapshot(pool_rig.root)

    # the pool's one member has no label, and the definition would need one
    response = post(client, edit_url("rebaser"), **{**FIELDS, "needs": "strong-model"})

    assert response.status_code == 400
    assert "pool 'rebasers'" in shown(response)
    assert "strong-model" in shown(response)
    # the fault names the real pool file, never the copy that was checked
    assert str(cli_admin.pool_dir(pool_rig.root) / pool_config.POOL_FILE) in shown(response)
    assert snapshot(pool_rig.root) == before


def test_a_save_into_a_directory_that_has_another_fault_is_refused_with_every_fault(root):
    (folder(root) / "critic.md").write_text("no front matter\n", encoding="utf-8")
    client = signed_in(create_app(root))
    before = snapshot(root)

    response = post(client, edit_url("worker"), **FIELDS)

    assert response.status_code == 400
    assert "definition 'critic'" in shown(response)
    assert str(folder(root) / "critic.md") in shown(response)
    assert snapshot(root) == before
    assert saves(root) == []


def test_a_save_that_mends_the_one_fault_of_the_directory_brings_the_pool_to_life(root):
    (folder(root) / "porter.md").write_text("---\nrole: Carries\n---\n", encoding="utf-8")
    app = create_app(root)
    assert app.state.pool is None and len(app.state.pool_errors) == 1
    client = signed_in(app)

    response = post(client, LIST + "/porter/edit", **FIELDS)

    assert response.status_code == 303, response.text
    assert app.state.pool is not None and app.state.pool_errors == ()
    assert PORTER in app.state.pool.config.definitions


def test_the_faults_of_a_refused_reload_show_until_a_save_reloads_the_directory(root):
    app = create_app(root)
    client = signed_in(app)
    path = folder(root) / "critic.md"
    good = path.read_text(encoding="utf-8")
    path.write_text("no front matter\n", encoding="utf-8")
    assert len(pool_routes.reload_pool(app, "developer")) == 1
    path.write_text(good, encoding="utf-8")

    listed = client.get(LIST)

    assert '<section id="directory-faults">' not in listed.text
    assert '<section id="reload-faults">' in listed.text
    assert "definition 'critic'" in shown(listed)
    assert post(client, LIST, **FIELDS).status_code == 303
    assert '<section id="reload-faults">' not in client.get(LIST).text


# -- delete ------------------------------------------------------------------------


def test_deleting_a_definition_that_a_pool_binds_is_refused_naming_the_pool(pool_rig):
    write_pool(pool_rig)
    client = signed_in(create_app(pool_rig.root))
    before = snapshot(pool_rig.root)

    response = post(client, delete_url("rebaser"))

    assert response.status_code == 409
    assert "pool 'rebasers' binds the definition 'rebaser'" in shown(response)
    assert snapshot(pool_rig.root) == before
    assert audit_records(pool_rig.root) == []


def test_deleting_a_definition_no_pool_binds_removes_the_file_and_reloads(root):
    app = create_app(root)
    client = signed_in(app)
    assert len(app.state.pool.config.definitions) == 5

    response = post(client, delete_url("critic"))

    assert response.status_code == 303, response.text
    assert response.headers["location"] == LIST
    assert not (folder(root) / "critic.md").exists()
    assert sorted(d.name for d in app.state.pool.config.definitions) == [
        "hermes-rebaser", "landscape-scanner", "model-update-checker", "worker",
    ]
    assert [(r["identity"], r["operation"], r["id"]) for r in audit_records(root)] == [
        ("developer", "definition_delete", "critic"),
        ("developer", "pool_reload", None),
    ]
    assert post(client, delete_url("critic")).status_code == 404


# -- a folder or a file that is a symlink ---------------------------------------------


LAYOUTS = ["folder", "file"]


def behind_a_link(folder: Path, elsewhere: Path, layout: str, name: str) -> Path:
    """Keep the file *name* of *folder* in *elsewhere* and leave a symlink in
    its place: to the whole folder for the layout "folder", to the one file
    for the layout "file". The real file is what comes back."""
    if layout == "folder":
        shutil.move(str(folder), str(elsewhere))
        folder.symlink_to(elsewhere, target_is_directory=True)
    else:
        elsewhere.mkdir()
        shutil.move(str(folder / name), str(elsewhere / name))
        (folder / name).symlink_to(elsewhere / name)
    return elsewhere / name


def kept(elsewhere: Path) -> dict[str, bytes]:
    """Every file of the real folder *elsewhere*, by name."""
    return {path.name: path.read_bytes() for path in sorted(elsewhere.iterdir())}


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_refused_save_leaves_the_real_file_behind_a_symlink_as_it_was(root, tmp_path, layout):
    elsewhere = tmp_path / "elsewhere"
    behind_a_link(folder(root), elsewhere, layout, "worker.md")
    client = signed_in(create_app(root))
    before = kept(elsewhere)

    response = post(client, edit_url("worker"), **{**FIELDS, "prompt": ""})

    assert response.status_code == 400
    assert EMPTY_BODY in shown(response)
    assert kept(elsewhere) == before
    assert saves(root) == []


def test_a_refused_create_makes_no_file_in_the_real_folder_behind_a_symlink(root, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    behind_a_link(folder(root), elsewhere, "folder", "worker.md")
    client = signed_in(create_app(root))
    before = kept(elsewhere)

    response = post(client, LIST, **{**FIELDS, "prompt": ""})

    assert response.status_code == 400
    assert EMPTY_BODY in shown(response)
    assert kept(elsewhere) == before
    assert not (folder(root) / "porter.md").exists()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_refused_delete_leaves_the_real_file_behind_a_symlink_in_place(root, tmp_path, layout):
    elsewhere = tmp_path / "elsewhere"
    behind_a_link(folder(root), elsewhere, layout, "worker.md")
    # another file has a fault, so the directory stands with no change at all
    (folder(root) / "critic.md").write_text("no front matter\n", encoding="utf-8")
    client = signed_in(create_app(root))
    before = kept(elsewhere)

    response = post(client, delete_url("worker"))

    assert response.status_code == 409
    assert "The definition 'worker' was not deleted" in shown(response)
    assert kept(elsewhere) == before
    assert (folder(root) / "worker.md").is_file()
    assert saves(root) == []


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_save_that_passed_changes_the_file_the_directory_shows_behind_a_symlink(
    root, tmp_path, layout
):
    behind_a_link(folder(root), tmp_path / "elsewhere", layout, "worker.md")
    client = signed_in(create_app(root))

    response = post(client, edit_url("worker"), **FIELDS)

    assert response.status_code == 303, response.text
    assert definitions.load_definition(folder(root) / "worker.md") == replace(PORTER, name="worker")
    assert [r["operation"] for r in saves(root)] == ["definition_save"]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_delete_that_passed_takes_the_file_out_of_the_directory_behind_a_symlink(
    root, tmp_path, layout
):
    behind_a_link(folder(root), tmp_path / "elsewhere", layout, "critic.md")
    client = signed_in(create_app(root))

    response = post(client, delete_url("critic"))

    assert response.status_code == 303, response.text
    assert "critic.md" not in [entry.name for entry in folder(root).iterdir()]
    assert [r["operation"] for r in saves(root)] == ["definition_delete"]


def test_the_check_of_a_candidate_reads_a_relative_symlink_of_a_linked_folder_as_it_was(
    root, tmp_path
):
    elsewhere = tmp_path / "elsewhere"
    behind_a_link(folder(root), elsewhere, "folder", "worker.md")
    # a file of the real folder that is a link with a path relative to that folder
    (tmp_path / "critic.md").write_bytes((elsewhere / "critic.md").read_bytes())
    (elsewhere / "critic.md").unlink()
    (elsewhere / "critic.md").symlink_to(Path("..") / "critic.md")
    text = (elsewhere / "worker.md").read_text(encoding="utf-8")

    faults = pool_manage.check_candidate(
        cli_admin.pool_dir(root), Path(pool_config.DEFINITIONS_DIR) / "worker.md", text
    )

    assert faults == []


# -- who may, and how ----------------------------------------------------------------


def test_the_requesters_session_gets_no_management_page(root):
    client = signed_in(create_app(root), "requester")

    for url in (LIST, NEW, edit_url("worker")):
        response = client.get(url)
        assert response.status_code == 403, url
        assert "developer identity only" in shown(response)
        assert "You are a worker." not in response.text


def test_the_requesters_posts_are_refused_and_change_nothing(root):
    client = signed_in(create_app(root), "requester")
    before = snapshot(root)

    for url in (LIST, edit_url("worker"), delete_url("worker")):
        response = post(client, url, **FIELDS)
        assert response.status_code == 403, url
        assert "developer identity only" in shown(response)
    assert snapshot(root) == before
    assert audit_records(root) == []


@pytest.mark.parametrize("identity", ["developer", "requester"])
def test_a_post_without_the_session_secret_is_refused_before_anything_else(root, identity):
    client = signed_in(create_app(root), identity)
    before = snapshot(root)

    for url in (LIST, edit_url("worker"), delete_url("worker")):
        for secret in ({}, {"session": "not-the-secret"}):
            response = client.post(url, data={**FIELDS, **secret})
            assert response.status_code == 403, url
            assert NOT_THIS_SESSION in shown(response)
    assert snapshot(root) == before
    assert audit_records(root) == []


def test_a_browser_with_no_session_is_sent_to_sign_in(root):
    client = TestClient(create_app(root), follow_redirects=False)

    for response in (client.get(LIST), client.get(NEW), client.post(LIST, data=FIELDS)):
        assert response.status_code == 303
        assert response.headers["location"] == web.SIGNIN_PATH
    assert not (folder(root) / "porter.md").exists()


def test_a_root_with_no_pool_directory_has_nothing_to_manage(tmp_path):
    client = signed_in(create_app(tmp_path / "daemon"))

    assert "No pool is configured on this daemon." in shown(client.get(LIST))
    response = post(client, LIST, **FIELDS)
    assert response.status_code == 409
    assert not cli_admin.pool_dir(tmp_path / "daemon").exists()


def test_the_management_routes_are_those_of_definitions_and_nothing_else():
    routes = sorted(
        (route.path, method) for route in pool_manage.pages.routes for method in route.methods
    )

    assert routes == [
        (LIST, "GET"),
        (LIST, "POST"),
        (NEW, "GET"),
        (LIST + "/{name}/delete", "POST"),
        (LIST + "/{name}/edit", "GET"),
        (LIST + "/{name}/edit", "POST"),
    ]
    assert web.DEFINITIONS_PATH == LIST


def test_every_form_posts_to_a_url_helper_and_the_list_links_back_to_the_pool():
    templates = {
        name: (web.TEMPLATES_DIR / name).read_text(encoding="utf-8")
        for name in ("pool_definitions.html", "pool_definition_form.html")
    }

    actions = {
        name: re.findall(r'<form[^>]*action="([^"]*)"', text) for name, text in templates.items()
    }
    assert actions == {
        "pool_definitions.html": ["{{ definition_delete_url(row.name) }}"],
        "pool_definition_form.html": ["{{ definition_save_url(name) }}"],
    }
    assert 'href="{{ pool_path }}"' in templates["pool_definitions.html"]
