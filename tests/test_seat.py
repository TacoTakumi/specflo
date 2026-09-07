"""The agent's seat: a client workspace under the daemon root, one per hosted project.

The daemon runs an agent for a hosted project as a CLI client of itself.
A client needs a checkout to run in: a config whose active project is the
hosted slug and a remote entry pointing at the daemon with an agent token.
The seat is that checkout and nothing more: no artifact lives in it, and
scaffolding it again changes nothing.
"""

import json

import pytest
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app
from specflo.daemon import auth, seat
from specflo.service.local import LocalProjectService

runner = CliRunner()


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


def hosted(root, name="Login fix"):
    """A project the daemon holds, with a brainstorm to read back."""
    service = LocalProjectService(root, config.load_config(root), actor="requester", hosted=True)
    project = service.create_project(name)
    service.start_brainstorm(project.slug)
    return project.slug


def test_scaffold_makes_a_client_checkout_bound_to_the_hosted_project(root):
    slug = hosted(root)

    workspace = seat.scaffold(root, slug, "http://127.0.0.1:8741", "agent-secret")

    assert workspace == seat.seat_dir(root, slug) == root / seat.SEATS_DIRNAME / slug
    cfg = config.load_config(workspace)
    assert cfg.active_project == slug
    assert config.hosting_remote(workspace, slug) == seat.REMOTE_NAME
    remote = config.load_remote(workspace, seat.REMOTE_NAME)
    assert remote.url == "http://127.0.0.1:8741" and remote.token == "agent-secret"
    assert config.list_remotes(workspace) == {seat.REMOTE_NAME: "http://127.0.0.1:8741"}


def test_the_seat_holds_no_projects_directory_and_no_artifact(root):
    slug = hosted(root)

    workspace = seat.scaffold(root, slug, "http://127.0.0.1:8741", "agent-secret")

    cfg = config.load_config(workspace)
    assert not (workspace / cfg.projects_dir).exists()
    assert not (workspace / "docs").exists()
    entries = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*") if p.is_file())
    assert entries == [
        ".specflo/config.yaml",
        ".specflo/hosted.json",
        ".specflo/remotes/.gitignore",
        f".specflo/remotes/{seat.REMOTE_NAME}.json",
    ]


def test_scaffolding_twice_leaves_one_identical_workspace(root):
    slug = hosted(root)
    first = seat.scaffold(root, slug, "http://127.0.0.1:8741", "agent-secret")
    snapshot = {p.relative_to(first).as_posix(): p.read_bytes() for p in first.rglob("*") if p.is_file()}

    again = seat.scaffold(root, slug, "http://127.0.0.1:8741", "agent-secret")

    assert again == first
    assert {p.relative_to(again).as_posix(): p.read_bytes() for p in again.rglob("*") if p.is_file()} == snapshot
    assert [p.name for p in (root / seat.SEATS_DIRNAME).iterdir()] == [slug]


def test_scaffolding_again_with_a_new_token_or_url_rebinds_the_remote(root):
    slug = hosted(root)
    seat.scaffold(root, slug, "http://127.0.0.1:8741", "old-secret")

    workspace = seat.scaffold(root, slug, "http://127.0.0.1:9000", "new-secret")

    remote = config.load_remote(workspace, seat.REMOTE_NAME)
    assert remote.url == "http://127.0.0.1:9000" and remote.token == "new-secret"


def test_the_token_is_kept_out_of_git_and_readable_by_the_owner_only(root):
    slug = hosted(root)

    workspace = seat.scaffold(root, slug, "http://127.0.0.1:8741", "agent-secret")

    remotes = workspace / ".specflo" / "remotes"
    assert (remotes / ".gitignore").read_text() == "*\n"
    assert (remotes / f"{seat.REMOTE_NAME}.json").stat().st_mode & 0o777 == 0o600


def test_scaffold_refuses_a_slug_that_is_not_one(root):
    from specflo.errors import SpecfloError

    with pytest.raises(SpecfloError):
        seat.scaffold(root, "../escape", "http://127.0.0.1:8741", "agent-secret")
    assert not (root / seat.SEATS_DIRNAME).exists()


def test_a_cli_run_from_the_seat_resolves_the_project_as_hosted(root, live_daemon, monkeypatch):
    slug = hosted(live_daemon["root"])
    token = auth.mint_token(live_daemon["root"], "agent")
    workspace = seat.scaffold(live_daemon["root"], slug, live_daemon["url"], token)
    monkeypatch.chdir(workspace)

    status = runner.invoke(app, ["status", "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["active_project"] == slug

    shown = runner.invoke(app, ["doc", "show", "brainstorm"])
    assert shown.exit_code == 0, shown.output
    expected = (live_daemon["root"] / daemon.PROJECTS_DIRNAME / slug / "brainstorm.md").read_text()
    assert shown.stdout == expected

    whoami = runner.invoke(app, ["decision", "add", "--text", "From the seat"])
    assert whoami.exit_code == 0, whoami.output
    assert "- Actor: agent" in (live_daemon["root"] / daemon.PROJECTS_DIRNAME / slug / "brainstorm.md").read_text()
    assert not (workspace / "docs").exists()
