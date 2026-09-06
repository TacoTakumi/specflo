"""The actor on daemon mutations.

Every daemon request runs as the identity its token was minted for. An
entry added through the daemon (a decision, requirement, task or milestone)
carries an Actor line naming that identity; the same add in local mode
carries none. The daemon also appends one audit record per mutation, so who
changed what is on file beside the artifacts.
"""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import brainstorm, config, daemon, plan, projects, spec
from specflo.cli import app
from specflo.daemon import auth, routes
from specflo.daemon.app import create_app
from specflo.service import wire
from specflo.service.local import LocalProjectService

runner = CliRunner()


def _entry(document: str, entry_id: str) -> str:
    """The lines of one ``### <id> ...`` entry, up to the next heading."""
    lines = document.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"### {entry_id} "))
    body = []
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        body.append(line)
    return "\n".join(body)


def _add_everything(service, slug):
    """Add one of each entry kind; returns their ids."""
    service.start_brainstorm(slug)
    decision = service.add_decision(slug, "Use one facade", rationale="one seam")
    service.start_spec(slug)
    requirement = service.add_requirement(slug, "Prints help", "a no-arg run exits 0")
    service.start_plan(slug)
    milestone = service.add_milestone(slug, "Help works", ["help prints"])
    task = service.add_task(
        slug, "Build help", "help prints", "uv run pytest", [requirement.id],
        milestone=milestone.id,
    )
    return decision.id, requirement.id, task.id, milestone.id


def _actor_lines(service, slug, ids):
    decision, requirement, task, milestone = ids
    return [
        _actor_of(_entry(service.show_document(slug, "brainstorm"), decision)),
        _actor_of(_entry(service.show_document(slug, "spec"), requirement)),
        _actor_of(_entry(service.show_document(slug, "plan"), task)),
        _actor_of(_entry(service.show_document(slug, "plan"), milestone)),
    ]


def _actor_of(entry: str) -> str | None:
    for line in entry.splitlines():
        if line.startswith("- Actor: "):
            return line.split(": ", 1)[1]
    return None


# --- the module functions ---------------------------------------------------


def test_module_adds_write_an_actor_line_only_when_given(tmp_path):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    brainstorm.start_brainstorm(tmp_path, cfg, "thing")
    spec.start_spec(tmp_path, cfg, "thing")
    plan.start_plan(tmp_path, cfg, "thing")

    plain = brainstorm.add_decision(tmp_path, cfg, "thing", "Plain")
    stamped = brainstorm.add_decision(tmp_path, cfg, "thing", "Stamped", actor="developer")
    requirement = spec.add_requirement(
        tmp_path, cfg, "thing", "Prints help", "exits 0", actor="requester"
    )
    milestone = plan.add_milestone(tmp_path, cfg, "thing", "Help works", ["it prints"], actor="developer")
    task = plan.add_task(
        tmp_path, cfg, "thing", "Build help", "prints", "pytest", [requirement.id],
        milestone=milestone.id, actor="developer",
    )

    document = brainstorm.brainstorm_path(tmp_path, cfg, "thing").read_text()
    assert _actor_of(_entry(document, plain.id)) is None
    assert _entry(document, stamped.id).splitlines() == [
        "- Rationale: —",
        "- Actor: developer",
        "- Status: active",
    ]
    spec_doc = spec.spec_path(tmp_path, cfg, "thing").read_text()
    assert _entry(spec_doc, requirement.id).splitlines()[-2:] == ["- Actor: requester", "- Status: active"]
    plan_doc = plan.plan_path(tmp_path, cfg, "thing").read_text()
    assert _entry(plan_doc, task.id).splitlines()[-3:] == [
        "- Actor: developer", "- Progress: pending", "- Status: active",
    ]
    assert _entry(plan_doc, milestone.id).splitlines() == [
        "- Exit:", "  - it prints", "- Actor: developer",
    ]
    # The parsers read the stamped entries exactly as they read plain ones.
    assert spec.validate_spec(tmp_path, cfg, "thing") == [
        "'In scope' section is empty.", "'Out of scope' section is empty.",
    ]
    assert [t.id for t in plan.list_tasks(tmp_path, cfg, "thing")] == [task.id]
    assert plan.milestone_detail(tmp_path, cfg, "thing", milestone.id)["exit_items"] == ["it prints"]
    assert plan.validate_plan(tmp_path, cfg, "thing") == []


# --- the services -----------------------------------------------------------


def test_local_service_stamps_every_add_with_its_actor_and_none_by_default(tmp_path):
    cfg = config.init_config(tmp_path)
    plain = LocalProjectService(tmp_path, cfg)
    stamped = LocalProjectService(tmp_path, cfg, actor="requester")

    plain.create_project("Plain")
    stamped.create_project("Stamped")

    assert _actor_lines(plain, "plain", _add_everything(plain, "plain")) == [None] * 4
    assert _actor_lines(stamped, "stamped", _add_everything(stamped, "stamped")) == ["requester"] * 4


@pytest.fixture
def daemon_root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


def _client(daemon_root, identity):
    client = TestClient(create_app(daemon_root))
    client.headers["Authorization"] = f"Bearer {auth.mint_token(daemon_root, identity)}"
    return client


def _call(client, operation, **kwargs):
    response = client.post(wire.route_path(operation), json=kwargs)
    assert response.status_code == 200, response.text
    return wire.decode(response.json()["result"], wire.OPERATIONS[operation].returns)


def test_daemon_adds_carry_the_identity_behind_the_token(daemon_root):
    requester = _client(daemon_root, "requester")
    developer = _client(daemon_root, "developer")
    _call(requester, "create_project", name="Thing")
    _call(requester, "start_brainstorm", slug="thing")

    asked = _call(requester, "add_decision", slug="thing", text="Asked for")
    decided = _call(developer, "add_decision", slug="thing", text="Decided")

    document = _call(developer, "show_document", slug="thing", name="brainstorm")
    assert _actor_of(_entry(document, asked.id)) == "requester"
    assert _actor_of(_entry(document, decided.id)) == "developer"


def test_local_mode_adds_carry_no_actor_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "Thing"])

    added = runner.invoke(app, ["decision", "add", "--text", "Local"])

    assert added.exit_code == 0, added.output
    document = (tmp_path / "docs" / "projects" / "thing" / "brainstorm.md").read_text()
    assert "- Actor:" not in document


def test_hosted_adds_through_the_cli_carry_the_identity(tmp_path, monkeypatch, live_daemon):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    config.init_config(checkout)
    monkeypatch.chdir(checkout)
    runner.invoke(app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]])
    runner.invoke(app, ["new", "Thing", "--remote", "home"])

    added = runner.invoke(app, ["decision", "add", "--text", "Hosted"])

    assert added.exit_code == 0, added.output
    document = (live_daemon["root"] / daemon.PROJECTS_DIRNAME / "thing" / "brainstorm.md").read_text()
    assert "- Actor: developer" in document
    assert "- Actor:" not in runner.invoke(app, ["doc", "show", "spec"]).output


# --- the audit log ----------------------------------------------------------


def test_daemon_appends_one_audit_record_per_mutation(daemon_root):
    client = _client(daemon_root, "requester")
    project = _call(client, "create_project", name="Thing")
    _call(client, "start_brainstorm", slug="thing")
    first = _call(client, "add_decision", slug="thing", text="One")
    second = _call(client, "add_decision", slug="thing", text="Two")
    _call(client, "show_document", slug="thing", name="brainstorm")
    _call(client, "list_projects")
    _call(client, "has_artifact", slug="thing", name="spec")
    # Derived writes render what the adds above already recorded: no record.
    _call(client, "write_checkpoint", slug="thing")
    _call(client, "write_index")

    records = [
        json.loads(line)
        for line in (daemon_root / routes.AUDIT_FILENAME).read_text().splitlines()
    ]

    assert [(r["identity"], r["project"], r["operation"], r["id"]) for r in records] == [
        ("requester", project.slug, "create_project", None),
        ("requester", "thing", "start_brainstorm", None),
        ("requester", "thing", "add_decision", first.id),
        ("requester", "thing", "add_decision", second.id),
    ]
    assert all(r["time"] for r in records)


def test_reads_and_mutations_are_classified_for_every_operation():
    assert routes.READ_OPERATIONS <= set(wire.OPERATIONS)
    assert {"add_decision", "add_task", "start_task", "close_round", "set_section"} <= routes.MUTATING_OPERATIONS
    assert {"show_document", "list_tasks", "build_status", "validate_artifact"} <= routes.READ_OPERATIONS
    assert routes.READ_OPERATIONS | routes.MUTATING_OPERATIONS == set(wire.OPERATIONS)
    assert routes.DERIVED_OPERATIONS <= routes.MUTATING_OPERATIONS
    assert routes.AUDITED_OPERATIONS | routes.DERIVED_OPERATIONS == routes.MUTATING_OPERATIONS
    assert "write_checkpoint" not in routes.AUDITED_OPERATIONS


def test_creating_a_project_runs_under_the_root_lock_whatever_its_scope():
    # `import_project` names its slug, but it creates a directory the same way
    # `create_project` does; both take the root lock so two creations of one
    # slug cannot both pass the existence check.
    assert routes.lock_slug(wire.OPERATIONS["import_project"], {"slug": "x", "files": {}}) is None
    assert routes.lock_slug(wire.OPERATIONS["create_project"], {"name": "X"}) is None
    assert routes.lock_slug(wire.OPERATIONS["add_decision"], {"slug": "x", "text": "t"}) == "x"
    assert routes.lock_slug(wire.OPERATIONS["list_projects"], {}) is None


def test_a_refused_mutation_leaves_no_audit_record(daemon_root):
    client = _client(daemon_root, "developer")

    response = client.post(wire.route_path("add_decision"), json={"slug": "nope", "text": "x"})

    assert response.status_code == 400
    assert not (daemon_root / routes.AUDIT_FILENAME).exists()
