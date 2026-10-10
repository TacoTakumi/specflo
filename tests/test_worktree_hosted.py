"""Task worktrees are refused on a project hosted on a daemon.

`task start --worktree` and `task land` each exit non-zero naming the
hosted project, before any request reaches the daemon.
"""

import pytest
from typer.testing import CliRunner

from specflo import config
from specflo.cli import app
from specflo.service import remote

runner = CliRunner()


@pytest.fixture
def hosted(tmp_path, monkeypatch, live_daemon):
    root = tmp_path / "checkout"
    root.mkdir()
    config.init_config(root)
    monkeypatch.chdir(root)
    added = runner.invoke(
        app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]]
    )
    assert added.exit_code == 0, added.output
    created = runner.invoke(app, ["new", "Thing", "--remote", "home"])
    assert created.exit_code == 0, created.output
    sent = []

    def record(self, operation, arguments):
        sent.append(operation)
        raise AssertionError(f"{operation} reached the daemon")

    monkeypatch.setattr(remote.RemoteProjectService, "_call", record)
    return sent


@pytest.mark.parametrize("args", [
    ["task", "start", "T-01", "--worktree"],
    ["task", "land", "T-01"],
])
def test_the_verb_is_refused_before_any_request(hosted, args):
    result = runner.invoke(app, args)

    assert result.exit_code != 0
    assert "'thing' is hosted on remote 'home'" in result.output
    assert hosted == []
