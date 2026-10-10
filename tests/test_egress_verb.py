"""The egress verb: pinning an egress class on the active project."""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import config, daemon, projects
from specflo.cli import app
from specflo.daemon import auth
from specflo.daemon.app import create_app
from specflo.daemon.permissions import EGRESS_SET, can, who_can
from specflo.service import wire

runner = CliRunner()


def _new_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["new", "Thing"]).exit_code == 0


def _egress_of(tmp_path, slug="thing"):
    root = config.find_root(tmp_path)
    return projects.load_project(root, config.load_config(root), slug).egress


def test_a_new_project_has_no_egress_pin(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)
    assert _egress_of(tmp_path) == ""
    assert "egress" not in (tmp_path / "docs" / "projects" / "thing" / "project.md").read_text()


def test_egress_verb_pins_each_class_on_the_active_project(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)
    for egress_class in ("local", "no-train", "open", "local"):
        result = runner.invoke(app, ["egress", egress_class])
        assert result.exit_code == 0, result.output
        assert result.output == f"Egress class for 'thing': {egress_class}\n"
        assert _egress_of(tmp_path) == egress_class


def test_the_project_record_shows_the_pinned_class(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)
    runner.invoke(app, ["egress", "local"])
    shown = runner.invoke(app, ["doc", "show", "project"])
    assert shown.exit_code == 0, shown.output
    assert "\negress: local\n" in shown.output


def test_egress_verb_reports_unchanged_when_the_class_matches(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)
    runner.invoke(app, ["egress", "local"])

    result = runner.invoke(app, ["egress", "local"])
    assert result.exit_code == 0
    assert "unchanged" in result.output
    assert _egress_of(tmp_path) == "local"


def test_egress_verb_json_emits_class_and_changed(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)

    first = runner.invoke(app, ["egress", "local", "--json"])
    assert first.exit_code == 0
    assert json.loads(first.output) == {"egress": "local", "changed": True}

    again = runner.invoke(app, ["egress", "local", "--json"])
    assert again.exit_code == 0
    assert json.loads(again.output) == {"egress": "local", "changed": False}


def test_egress_verb_refuses_an_unknown_class(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)
    runner.invoke(app, ["egress", "local"])

    result = runner.invoke(app, ["egress", "public"])
    assert result.exit_code != 0
    assert "'public'" in result.output
    assert all(name in result.output for name in ("local", "no-train", "open"))
    assert _egress_of(tmp_path) == "local"


def test_egress_verb_requires_an_active_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["egress", "local"])
    assert result.exit_code != 0


def test_the_pin_survives_shelve_resume_and_advance_through_the_cli(tmp_path, monkeypatch):
    _new_project(tmp_path, monkeypatch)
    runner.invoke(app, ["egress", "local"])

    assert runner.invoke(app, ["shelve", "--reason", "later"]).exit_code == 0
    assert _egress_of(tmp_path) == "local"
    assert runner.invoke(app, ["resume", "thing"]).exit_code == 0
    assert _egress_of(tmp_path) == "local"

    root = config.find_root(tmp_path)
    projects.advance_project(root, config.load_config(root), "thing")
    assert _egress_of(tmp_path) == "local"


# --- the pin on a hosted project: the developer's alone ----------------------


def test_the_developer_alone_holds_egress_set():
    assert can("developer", EGRESS_SET)
    assert not can("requester", EGRESS_SET)
    assert not can("agent", EGRESS_SET)
    assert who_can(EGRESS_SET) == "the developer role"


@pytest.fixture
def hosted(tmp_path):
    """A daemon root holding the project 'thing', pinned local, and a client
    for each of rob (a developer), ann (a requester) and the agent."""
    root = daemon.prepare_root(tmp_path / "daemon")
    auth.add_user(root, "rob", "developer", None)
    auth.add_user(root, "ann", "requester", None)
    clients = {}
    for owner in ("rob", "ann", "agent"):
        client = TestClient(create_app(root))
        client.headers["Authorization"] = f"Bearer {auth.mint_user_token(root, owner, 'seat-x')}"
        clients[owner] = client
    assert clients["rob"].post(wire.route_path("create_project"), json={"name": "Thing"}).status_code == 200
    assert _set_egress(clients["rob"], "thing", "local").status_code == 200
    return root, clients


def _set_egress(client, slug, egress_class):
    return client.post(
        wire.route_path("set_egress"), json={"slug": slug, "egress_class": egress_class}
    )


def _hosted_egress(root, slug="thing"):
    return projects.load_project(root, config.load_config(root), slug).egress


def test_a_developer_token_sets_the_egress_pin(hosted):
    root, clients = hosted

    response = _set_egress(clients["rob"], "thing", "open")

    assert response.status_code == 200, response.text
    assert _hosted_egress(root) == "open"


@pytest.mark.parametrize("owner", ["ann", "agent"])
def test_egress_set_refuses_any_role_but_the_developer(hosted, owner):
    root, clients = hosted

    response = _set_egress(clients[owner], "thing", "open")

    assert response.status_code == 403, response.text
    assert "the developer role" in response.json()["detail"]
    assert _hosted_egress(root) == "local"
