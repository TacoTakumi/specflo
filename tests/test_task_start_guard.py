"""`task start` refuses a task whose dependencies are not all done.

The refusal names each unmet dependency and changes nothing. ``--force``
starts the task anyway. A daemon gives the same refusal through the service
protocol, so a hosted project is guarded the same way.
"""

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import config, daemon, plan, projects, spec
from specflo.cli import app
from specflo.daemon.app import create_app
from specflo.errors import SpecfloError
from specflo.service.local import LocalProjectService
from specflo.service.remote import RemoteProjectService

from conftest import role_token

runner = CliRunner()


def _two_tasks(root, cfg, slug="thing"):
    """A plan where the second task depends on the first."""
    spec.start_spec(root, cfg, slug)
    spec.add_requirement(root, cfg, slug, "Prints help", acceptance="exits 0")
    plan.start_plan(root, cfg, slug)
    first = plan.add_task(root, cfg, slug, "First", "a", "uv run pytest", implements=["REQ-01"])
    second = plan.add_task(root, cfg, slug, "Second", "b", "uv run pytest",
                           implements=["REQ-01"], depends_on=[first.id])
    return first.id, second.id


@pytest.fixture
def planned(tmp_path, monkeypatch):
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, "Thing")
    first, second = _two_tasks(tmp_path, cfg)
    projects.switch_project(tmp_path, cfg, "thing")
    monkeypatch.chdir(tmp_path)
    return tmp_path, cfg, first, second


def _progress(root, cfg, task_id):
    return next(t.progress for t in plan.list_tasks(root, cfg, "thing") if t.id == task_id)


def test_start_is_refused_while_a_dependency_is_pending(planned):
    root, cfg, first, second = planned
    path = plan.plan_path(root, cfg, "thing")
    before = path.read_bytes()

    result = runner.invoke(app, ["task", "start", second])

    assert result.exit_code != 0
    assert first in result.output and "--force" in result.output
    assert path.read_bytes() == before


def test_force_starts_it_anyway(planned):
    root, cfg, first, second = planned

    result = runner.invoke(app, ["task", "start", second, "--force"])

    assert result.exit_code == 0, result.output
    assert _progress(root, cfg, second) == "in_progress"
    assert _progress(root, cfg, first) == "pending"


def test_start_goes_through_once_the_dependency_is_done(planned):
    root, cfg, first, second = planned
    assert runner.invoke(app, ["task", "start", first]).exit_code == 0
    assert runner.invoke(app, ["task", "done", first]).exit_code == 0

    result = runner.invoke(app, ["task", "start", second])

    assert result.exit_code == 0, result.output
    assert _progress(root, cfg, second) == "in_progress"


def test_an_in_progress_dependency_is_not_met(planned):
    root, cfg, first, second = planned
    assert runner.invoke(app, ["task", "start", first]).exit_code == 0

    result = runner.invoke(app, ["task", "start", second])

    assert result.exit_code != 0 and first in result.output


def test_the_daemon_gives_the_same_refusal(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")
    local = LocalProjectService(root, config.load_config(root), hosted=True)
    local.create_project("Thing")
    first, second = _two_tasks(root, config.load_config(root))
    client = TestClient(create_app(root))
    remote = RemoteProjectService("http://testserver", role_token(root, "developer"),
                                  client=client)
    path = plan.plan_path(root, config.load_config(root), "thing")
    before = path.read_bytes()

    with pytest.raises(SpecfloError, match=first):
        remote.start_task("thing", second)
    assert path.read_bytes() == before

    assert remote.start_task("thing", second, force=True).progress == "in_progress"
