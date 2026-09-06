"""Per-project locality: a project lives in the checkout or on one daemon.

``specflo new --remote <name>`` creates a project on that daemon and records
which remote holds it in the checkout's hosted registry; nothing for it is
written under the checkout. ``list`` shows each project's locality, the
active pointer may name either kind, and every command routes by the active
project's locality through one resolver.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app
from specflo.service.local import LocalProjectService
from specflo.service.remote import RemoteProjectService
from specflo.service.resolve import local_service, remote_service, resolve_service

runner = CliRunner()


@pytest.fixture
def checkout(tmp_path, monkeypatch, live_daemon):
    """A checkout with the live daemon registered as remote ``home``."""
    root = tmp_path / "checkout"
    root.mkdir()
    config.init_config(root)
    monkeypatch.chdir(root)
    done = runner.invoke(
        app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]]
    )
    assert done.exit_code == 0, done.output
    return root


def _files_naming(root, slug):
    """Every path under ``root`` with ``slug`` as a path component."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if slug in p.parts)


# --- the hosted registry ----------------------------------------------------


def test_hosted_registry_round_trips_slug_to_remote(tmp_path):
    config.init_config(tmp_path)
    assert config.hosted_projects(tmp_path) == {}

    config.record_hosted_project(tmp_path, "thing", "home")
    config.record_hosted_project(tmp_path, "other", "work")

    assert config.hosted_projects(tmp_path) == {"other": "work", "thing": "home"}
    assert config.hosting_remote(tmp_path, "thing") == "home"
    assert config.hosting_remote(tmp_path, "nope") is None
    assert config.forget_hosted_project(tmp_path, "thing") is True
    assert config.forget_hosted_project(tmp_path, "thing") is False
    assert config.hosted_projects(tmp_path) == {"other": "work"}


# --- the resolver -----------------------------------------------------------


def test_resolver_picks_the_service_by_the_projects_locality(tmp_path):
    cfg = config.init_config(tmp_path)
    config.add_remote(tmp_path, "home", "http://127.0.0.1:9", "token")
    config.record_hosted_project(tmp_path, "hosted-one", "home")

    assert isinstance(local_service(tmp_path, cfg), LocalProjectService)
    assert isinstance(remote_service(tmp_path, "home"), RemoteProjectService)
    assert isinstance(resolve_service(tmp_path, cfg, "local-one"), LocalProjectService)
    assert isinstance(resolve_service(tmp_path, cfg, "hosted-one"), RemoteProjectService)
    assert isinstance(resolve_service(tmp_path, cfg), LocalProjectService)
    cfg.active_project = "hosted-one"
    assert isinstance(resolve_service(tmp_path, cfg), RemoteProjectService)
    assert resolve_service(tmp_path, cfg).url == "http://127.0.0.1:9"


# --- new --remote -----------------------------------------------------------


def test_new_remote_creates_the_project_on_the_daemon_and_nothing_in_the_checkout(
    checkout, live_daemon
):
    result = runner.invoke(app, ["new", "Hosted Thing", "--remote", "home"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[:2] == [
        "Created project 'hosted-thing' (now active). Phase: brainstorm.",
        "Scaffolded hosted-thing/brainstorm (ready to work).",
    ]
    hosted = live_daemon["root"] / daemon.PROJECTS_DIRNAME / "hosted-thing"
    assert (hosted / "project.md").is_file()
    assert (hosted / "brainstorm.md").is_file()
    assert (hosted / "checkpoint.md").is_file()
    assert _files_naming(checkout, "hosted-thing") == []
    assert config.hosted_projects(checkout) == {"hosted-thing": "home"}
    assert config.load_config(checkout).active_project == "hosted-thing"


def test_new_remote_refuses_an_unknown_remote(checkout):
    result = runner.invoke(app, ["new", "Thing", "--remote", "nowhere"])

    assert result.exit_code == 1
    assert "No remote 'nowhere'" in result.stderr
    assert _files_naming(checkout, "thing") == []


def test_one_slug_has_one_locality(checkout):
    assert runner.invoke(app, ["new", "Alpha"]).exit_code == 0
    assert runner.invoke(app, ["new", "Bravo", "--remote", "home"]).exit_code == 0

    local_again = runner.invoke(app, ["new", "Bravo"])
    assert local_again.exit_code == 1
    assert "hosted on remote 'home'" in local_again.stderr

    remote_again = runner.invoke(app, ["new", "Alpha", "--remote", "home"])
    assert remote_again.exit_code == 1
    assert "already exists" in remote_again.stderr


# --- list, switch, status across localities --------------------------------


def test_list_shows_the_locality_of_every_project(checkout):
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[:2] == [
        "  alpha  (brainstorm)",
        "* bravo  (brainstorm)  [hosted: home]",
    ]

    data = json.loads(runner.invoke(app, ["list", "--json"]).output)
    by_slug = {p["slug"]: p for p in data["projects"]}
    assert by_slug["alpha"]["locality"] == "local" and by_slug["alpha"]["remote"] is None
    assert by_slug["bravo"]["locality"] == "hosted" and by_slug["bravo"]["remote"] == "home"
    assert by_slug["bravo"]["phase"] == "brainstorm" and by_slug["bravo"]["active"] is True


def test_switch_and_status_work_across_localities(checkout, live_daemon):
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])

    switched = runner.invoke(app, ["switch", "Alpha"])
    assert switched.exit_code == 0
    assert "Switched to 'alpha' (phase: brainstorm)." in switched.output
    status = runner.invoke(app, ["status", "--json"])
    assert json.loads(status.output)["active_project"] == "alpha"

    switched = runner.invoke(app, ["switch", "Bravo"])
    assert switched.exit_code == 0
    assert "Switched to 'bravo' (phase: brainstorm)." in switched.output
    status = runner.invoke(app, ["status", "--json"])
    payload = json.loads(status.output)
    assert payload["active_project"] == "bravo" and payload["phase"] == "brainstorm"
    assert payload["dir"].startswith(str(live_daemon["root"]))


def test_commands_on_the_active_hosted_project_reach_the_daemon(checkout, live_daemon):
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])

    added = runner.invoke(app, ["decision", "add", "--text", "Use one facade"])
    assert added.exit_code == 0, added.output
    assert added.output.startswith("Recorded ")

    shown = runner.invoke(app, ["doc", "show", "brainstorm"])
    assert shown.exit_code == 0
    assert "Use one facade" in shown.output
    brainstorm = live_daemon["root"] / daemon.PROJECTS_DIRNAME / "bravo" / "brainstorm.md"
    assert "Use one facade" in brainstorm.read_text()
    assert _files_naming(checkout, "bravo") == []


def test_summary_shelve_and_resume_route_by_the_named_projects_locality(checkout, live_daemon):
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])
    runner.invoke(app, ["switch", "Alpha"])

    summary = runner.invoke(app, ["summary", "Bravo", "Hosted and summarized"])
    assert summary.exit_code == 0, summary.output
    project = live_daemon["root"] / daemon.PROJECTS_DIRNAME / "bravo" / "project.md"
    assert "Hosted and summarized" in project.read_text()

    shelved = runner.invoke(app, ["shelve", "Bravo", "--reason", "later"])
    assert shelved.exit_code == 0, shelved.output
    assert "status: shelved" in project.read_text()
    resumed = runner.invoke(app, ["resume", "Bravo"])
    assert resumed.exit_code == 0, resumed.output
    assert "status: active" in project.read_text()
    assert config.load_config(checkout).active_project == "bravo"


def test_list_reports_an_unreachable_remote_without_failing(checkout):
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["remote", "add", "dead", "http://127.0.0.1:9", "--token", "x"])
    config.record_hosted_project(checkout, "ghost", "dead")

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "  ghost  (?)  [hosted: dead, unreachable]" in result.output.splitlines()
    assert "note:" in result.stderr and "127.0.0.1:9" in result.stderr
    data = json.loads(runner.invoke(app, ["list", "--json"]).output)
    ghost = next(p for p in data["projects"] if p["slug"] == "ghost")
    assert ghost["locality"] == "hosted" and ghost["remote"] == "dead"
    assert ghost["phase"] is None and "error" in ghost


def test_index_and_the_projects_dir_guard_read_the_checkout_only(checkout, live_daemon):
    runner.invoke(app, ["new", "Alpha"])
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])

    result = runner.invoke(app, ["index"])

    assert result.exit_code == 0, result.output
    assert "(1 project)" in result.output
    assert not (live_daemon["root"] / daemon.PROJECTS_DIRNAME / "specflo-index.md").exists()


def test_a_hosted_advance_names_the_checkpoint_by_locator_and_carries_no_path(
    checkout, live_daemon
):
    runner.invoke(app, ["new", "Hosted Thing", "--remote", "home"])
    runner.invoke(app, ["brainstorm", "start"])
    runner.invoke(app, ["decision", "add", "--text", "one", "--rationale", "why"])
    runner.invoke(
        app, ["section", "set", "brainstorm", "Out of scope / Deferred", "--stdin"], input="No auth.\n"
    )

    advanced = runner.invoke(app, ["advance"])

    assert advanced.exit_code == 0, advanced.output
    assert "Checkpoint saved: hosted-thing/checkpoint" in advanced.output
    assert str(live_daemon["root"]) not in advanced.output
    reopened = json.loads(runner.invoke(app, ["reopen", "--json"]).output)
    assert reopened["checkpoint"] is None
    assert reopened["checkpoint_locator"] == "hosted-thing/checkpoint"
    assert str(live_daemon["root"]) not in json.dumps(reopened)


def test_list_opens_one_client_per_remote(checkout, live_daemon, monkeypatch):
    from specflo import cli as cli_module

    runner.invoke(app, ["new", "Alpha", "--remote", "home"])
    runner.invoke(app, ["new", "Bravo", "--remote", "home"])
    opened = []
    real = cli_module.remote_service

    def counting(root, name, **kwargs):
        opened.append(name)
        return real(root, name, **kwargs)

    monkeypatch.setattr(cli_module, "remote_service", counting)

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0, result.output
    assert opened == ["home"]
