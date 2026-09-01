"""v2 statefile parity (pi-interactive-transport T-03, REQ-07).

The extension's control surface writes the same per-agent disk layout the v1
host writes. A Node driver runs the control module under its fake harness
through one scripted run, then this suite reads the resulting files with the
v1 statefiles reader and the CLI code paths that consume them:

- events.jsonl parses line by line, agent_start lands before agent_settled,
  and every line carries a ``ts`` like v1's EventLog lines.
- status.json is read by ``read_status`` and carries the v1 core field set
  (the ``status_snapshot`` keys) with a valid lifecycle state.
- ``specflo agent log`` prints the log; ``specflo agent status`` takes its
  disk-fallback path and reports the same probe shape v1 produces for a host
  that is no longer running.

``specflo agent last`` needs the live socket protocol (T-04) and is pinned by
the shared contract suite (T-06), not here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from specflo import extension_install
from specflo.agent.statefiles import (
    ENV_STATE_DIR,
    LIFECYCLE_STATES,
    AgentPaths,
    read_status,
    status_snapshot,
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to drive the extension"
)


@pytest.fixture(scope="module")
def v2_state(tmp_path_factory):
    """A state base dir populated by the extension via the Node driver."""
    base = tmp_path_factory.mktemp("v2-state")
    cwd = tmp_path_factory.mktemp("served-project")
    driver = extension_install.extension_source() / "test" / "drive-statefiles.ts"
    env = {**os.environ, ENV_STATE_DIR: str(base)}
    env.pop("SPECFLO_AGENT_SERVE", None)
    env.pop("SPECFLO_AGENT_NAME", None)
    env.pop("SPECFLO_AGENT_MANAGED", None)
    result = subprocess.run(
        ["node", str(driver)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"driver failed: {result.stderr}"
    return {"base": base, "name": cwd.name}


def run_cli(*args: str, base: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from specflo.cli import main; sys.exit(main())",
            "agent",
            *args,
        ],
        env={**os.environ, ENV_STATE_DIR: str(base)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_v2_statefiles_event_log_is_v1_shaped(v2_state):
    paths = AgentPaths.resolve(v2_state["name"], base_dir=v2_state["base"])
    lines = paths.events.read_text().splitlines()
    events = [json.loads(line) for line in lines if line]
    types = [event["type"] for event in events]
    assert types.index("agent_start") < types.index("agent_settled")
    assert "message_end" in types
    assert "tool_execution_end" in types
    for event in events:
        assert isinstance(event.get("ts"), str), f"missing ts on {event['type']}"


def test_v2_statefiles_status_reads_with_the_v1_reader(v2_state):
    paths = AgentPaths.resolve(v2_state["name"], base_dir=v2_state["base"])
    snapshot = read_status(paths.status)
    v1_keys = set(status_snapshot(v2_state["name"], "idle"))
    assert v1_keys <= set(snapshot), f"missing v1 core fields: {v1_keys - set(snapshot)}"
    assert snapshot["state"] in LIFECYCLE_STATES
    assert snapshot["state"] == "idle"  # the scripted run settled
    assert snapshot["name"] == v2_state["name"]


def test_v2_statefiles_agent_log_prints_the_event_log(v2_state):
    result = run_cli("log", v2_state["name"], base=v2_state["base"])
    assert result.returncode == 0, result.stderr
    paths = AgentPaths.resolve(v2_state["name"], base_dir=v2_state["base"])
    assert result.stdout == paths.events.read_text()


def test_v2_statefiles_agent_status_disk_fallback_shape(v2_state):
    # The driver process is gone, so the probe takes the same disk-fallback
    # path it takes for a dead v1 host: exit 12, state "dead", v1 fields shown.
    result = run_cli("status", v2_state["name"], "--json", base=v2_state["base"])
    assert result.returncode == 12, result.stderr
    probe = json.loads(result.stdout)
    assert probe["name"] == v2_state["name"]
    assert probe["state"] == "dead"
    assert probe["alive"] is False
    v1_keys = set(status_snapshot(v2_state["name"], "idle"))
    assert v1_keys <= set(probe["status"])
