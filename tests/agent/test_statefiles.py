"""T-02: per-agent state dir layout, events.jsonl writer, atomic status.json."""

from __future__ import annotations

import json

import pytest

from specflo.agent.statefiles import (
    ENV_STATE_DIR,
    AgentPaths,
    EventLog,
    read_status,
    status_snapshot,
    write_status,
)

# -- layout -----------------------------------------------------------------


def test_paths_derivable_from_name_alone(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_STATE_DIR, str(tmp_path))
    paths = AgentPaths.resolve("worker-1")
    assert paths.root == tmp_path / "worker-1"
    assert paths.socket == tmp_path / "worker-1" / "sock"
    assert paths.events == tmp_path / "worker-1" / "events.jsonl"
    assert paths.status == tmp_path / "worker-1" / "status.json"
    # resolving the same name again lands on the same paths
    assert AgentPaths.resolve("worker-1") == paths


def test_explicit_base_dir_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_STATE_DIR, str(tmp_path / "env"))
    paths = AgentPaths.resolve("a1", base_dir=tmp_path / "explicit")
    assert paths.root == tmp_path / "explicit" / "a1"


def test_default_base_dir_without_env(monkeypatch):
    monkeypatch.delenv(ENV_STATE_DIR, raising=False)
    paths = AgentPaths.resolve("a1")
    assert paths.root.parts[-3:] == (".specflo", "agents", "a1")


@pytest.mark.parametrize("bad", ["", "../evil", "a/b", ".hidden", "sp ace", "-lead"])
def test_invalid_agent_names_rejected(bad):
    with pytest.raises(ValueError):
        AgentPaths.resolve(bad)


def test_ensure_creates_root(tmp_path):
    paths = AgentPaths.resolve("a1", base_dir=tmp_path).ensure()
    assert paths.root.is_dir()
    paths.ensure()  # idempotent


# -- events.jsonl -----------------------------------------------------------


def read_events(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").split("\n")
        if line
    ]


def test_event_log_appends_timestamped_lines(tmp_path):
    paths = AgentPaths.resolve("a1", base_dir=tmp_path).ensure()
    with EventLog(paths.events) as log:
        log.append({"type": "agent_start"})
        log.append({"type": "agent_settled", "detail": "d\u2028d"})

    events = read_events(paths.events)
    assert [e["type"] for e in events] == ["agent_start", "agent_settled"]
    for event in events:
        assert "ts" in event and event["ts"].endswith("+00:00")
    assert events[0]["ts"] <= events[1]["ts"]
    assert events[1]["detail"] == "d\u2028d"
    # one JSON object per line: exactly one LF per record
    assert paths.events.read_bytes().count(b"\n") == 2


def test_event_log_reopening_appends_not_truncates(tmp_path):
    paths = AgentPaths.resolve("a1", base_dir=tmp_path).ensure()
    with EventLog(paths.events) as log:
        log.append({"type": "host_start"})
    with EventLog(paths.events) as log:
        log.append({"type": "host_stop"})

    events = read_events(paths.events)
    assert [e["type"] for e in events] == ["host_start", "host_stop"]


# -- status.json ------------------------------------------------------------


def test_status_snapshot_carries_req05_field_set():
    snap = status_snapshot("a1", "idle", host_pid=100, pi_pid=200)
    assert set(snap) == {
        "name",
        "state",
        "host_pid",
        "pi_pid",
        "context_percent",
        "herdr_workspace",
        "herdr_tab",
        "herdr_pane",
        "last_activity",
    }
    assert snap["name"] == "a1"
    assert snap["state"] == "idle"
    assert snap["host_pid"] == 100
    assert snap["pi_pid"] == 200
    assert snap["context_percent"] is None
    assert snap["herdr_pane"] is None
    assert snap["last_activity"].endswith("+00:00")


def test_status_snapshot_rejects_unknown_state():
    with pytest.raises(ValueError):
        status_snapshot("a1", "running")


def test_write_status_atomic_replace(tmp_path):
    paths = AgentPaths.resolve("a1", base_dir=tmp_path).ensure()
    write_status(paths.status, status_snapshot("a1", "starting", host_pid=1))
    write_status(
        paths.status,
        status_snapshot(
            "a1",
            "working",
            host_pid=1,
            pi_pid=2,
            context_percent=30.5,
            herdr_workspace="ws1",
            herdr_tab="tab2",
            herdr_pane="pane3",
        ),
    )

    snap = read_status(paths.status)
    assert snap["state"] == "working"
    assert snap["context_percent"] == 30.5
    assert snap["herdr_workspace"] == "ws1"
    assert snap["herdr_tab"] == "tab2"
    assert snap["herdr_pane"] == "pane3"
    # the tmp file used for the atomic rename is gone
    assert [p.name for p in paths.root.iterdir()] == ["status.json"]
