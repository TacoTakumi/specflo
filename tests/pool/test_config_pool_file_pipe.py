"""A pool file or llama-swap file that is a named pipe is refused, not read.

Opening a named pipe that no process writes to waits for ever, and whoever
reads the pool configuration would wait with it: the daemon's start and
reload, ``pool validate``, the pool page and the plan read. So each path is
checked for what it is before anything opens it, and every one of those
answers at once with a fault naming the path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo.cli import app as cli
from specflo.daemon import pool_routes
from specflo.daemon.app import create_app
from specflo.pool import cli_admin, planneeds
from specflo.pool import config as pool_config

from .odd_entries import within
from .test_lease_request import write_pool

LIMIT = 5.0
FILES = ("pool", "llama-swap")


def piped(rig, which: str) -> Path:
    """The rig's pool directory with the *which* file replaced by a named pipe."""
    write_pool(rig)
    directory = cli_admin.pool_dir(rig.root)
    name = pool_config.POOL_FILE if which == "pool" else "llama-swap.yaml"
    pipe = directory / name
    pipe.unlink()
    os.mkfifo(pipe)
    return pipe


def named(faults, pipe: Path) -> bool:
    return any(str(pipe) in str(fault) and "not a regular file" in str(fault) for fault in faults)


@pytest.mark.parametrize("which", FILES)
def test_the_configuration_load_returns_a_fault_naming_the_pipe(pool_rig, which):
    pipe = piped(pool_rig, which)

    _, faults = within(
        pool_config.load_pool_config, cli_admin.pool_dir(pool_rig.root),
        pipes=[pipe], limit=LIMIT,
    )

    assert named(faults, pipe)


@pytest.mark.parametrize("which", FILES)
def test_pool_validate_reports_the_pipe(pool_rig, which):
    pipe = piped(pool_rig, which)

    result = within(
        CliRunner().invoke, cli, ["serve", "--root", str(pool_rig.root), "pool", "validate"],
        pipes=[pipe], limit=LIMIT,
    )

    assert result.exit_code == 1
    assert str(pipe) in result.output and "not a regular file" in result.output


@pytest.mark.parametrize("which", FILES)
def test_the_daemon_starts_with_the_fault_and_its_page_shows_it(pool_rig, which):
    from test_web_pool import page, signed_in

    pipe = piped(pool_rig, which)

    app = within(create_app, pool_rig.root, pipes=[pipe], limit=LIMIT)
    html = within(page, signed_in(app), pipes=[pipe], limit=LIMIT)

    assert app.state.pool is None
    assert named(app.state.pool_errors, pipe)
    assert str(pipe) in html and "not a regular file" in html


@pytest.mark.parametrize("which", FILES)
def test_a_reload_answers_with_the_fault(pool_rig, which):
    write_pool(pool_rig)
    app = create_app(pool_rig.root)
    pipe = cli_admin.pool_dir(pool_rig.root) / (
        pool_config.POOL_FILE if which == "pool" else "llama-swap.yaml"
    )
    pipe.unlink()
    os.mkfifo(pipe)

    faults = within(pool_routes.reload_pool, app, "developer", pipes=[pipe], limit=LIMIT)

    assert named(faults, pipe)


@pytest.mark.parametrize("which", FILES)
def test_the_plan_read_answers_with_no_pools(pool_rig, which):
    pipe = piped(pool_rig, which)

    assert within(planneeds.daemon_pools, pool_rig.root, pipes=[pipe], limit=LIMIT) == {}
