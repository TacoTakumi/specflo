"""T-10: herdr adapter against a fake herdr binary - sequences and unavailability."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from specflo.agent.herdr import (
    HerdrAdapter,
    HerdrError,
    HerdrPlacement,
    HerdrUnavailableError,
)

FAKE_HERDR = '''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_HERDR_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")

if os.environ.get("FAKE_HERDR_DOWN"):
    sys.stderr.write("error: cannot connect to herdr server\\n")
    sys.exit(1)

def out(result):
    print(json.dumps({"id": "cli", "result": result}))

if args[:2] == ["workspace", "list"]:
    workspaces = json.loads(os.environ.get("FAKE_HERDR_WORKSPACES", "[]"))
    out({"type": "workspace_list", "workspaces": workspaces})
elif args[:2] == ["workspace", "create"]:
    label = args[args.index("--label") + 1]
    out({"type": "workspace_created",
         "workspace": {"workspace_id": "wNEW", "label": label}})
elif args[:2] == ["tab", "create"]:
    ws = args[args.index("--workspace") + 1]
    out({"type": "tab_created",
         "tab": {"tab_id": ws + ":t9", "workspace_id": ws},
         "root_pane": {"pane_id": ws + ":p9", "tab_id": ws + ":t9"}})
elif args[:2] in (["pane", "run"], ["pane", "report-agent"],
                  ["pane", "release-agent"], ["tab", "close"]):
    out({"type": "ok"})
else:
    sys.stderr.write("unknown command\\n")
    sys.exit(2)
'''


@pytest.fixture
def fake_herdr(tmp_path, monkeypatch):
    """A fake herdr on PATH that logs argv and answers canned JSON."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "herdr"
    script.write_text(FAKE_HERDR, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HERDR_LOG", str(log))

    def calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").split("\n")
            if line
        ]

    return calls


def test_available_true_when_server_answers(fake_herdr):
    adapter = HerdrAdapter()
    assert adapter.available() is True
    assert fake_herdr() == [["workspace", "list"]]


def test_available_false_when_binary_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    adapter = HerdrAdapter()
    assert adapter.available() is False


def test_available_false_when_server_down(fake_herdr, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_DOWN", "1")
    assert HerdrAdapter().available() is False


def test_actions_raise_unavailable_when_binary_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    adapter = HerdrAdapter()
    with pytest.raises(HerdrUnavailableError):
        adapter.ensure_workspace("agents")
    with pytest.raises(HerdrUnavailableError):
        adapter.report_state("w1:p1", "a1", "working")


def test_ensure_workspace_reuses_existing_label(fake_herdr, monkeypatch):
    monkeypatch.setenv(
        "FAKE_HERDR_WORKSPACES",
        json.dumps([{"workspace_id": "w7", "label": "other"},
                    {"workspace_id": "w9", "label": "agents"}]),
    )
    adapter = HerdrAdapter()
    assert adapter.ensure_workspace("agents") == "w9"
    assert fake_herdr() == [["workspace", "list"]]  # no create issued


def test_ensure_workspace_creates_when_absent(fake_herdr):
    adapter = HerdrAdapter()
    assert adapter.ensure_workspace("agents") == "wNEW"
    assert fake_herdr() == [
        ["workspace", "list"],
        ["workspace", "create", "--label", "agents", "--no-focus"],
    ]


def test_placement_and_lifecycle_sequence(fake_herdr):
    adapter = HerdrAdapter()
    workspace_id = adapter.ensure_workspace("agents")
    placement = adapter.create_tab(workspace_id, "builder", "/work/dir")
    assert placement == HerdrPlacement(
        workspace_id="wNEW", tab_id="wNEW:t9", pane_id="wNEW:p9"
    )
    adapter.run_in_pane(placement.pane_id, "exec host-cmd --flag")
    adapter.report_state(placement.pane_id, "builder", "working", seq=1)
    adapter.report_state(placement.pane_id, "builder", "idle", seq=2)
    adapter.release(placement.pane_id, "builder")
    adapter.close_tab(placement.tab_id)

    assert fake_herdr()[2] == [
        "tab", "create", "--workspace", "wNEW", "--label", "builder",
        "--cwd", "/work/dir", "--no-focus",
    ]
    assert fake_herdr()[3:] == [
        ["pane", "run", "wNEW:p9", "exec host-cmd --flag"],
        # pane id first, per the T-09 probe
        ["pane", "report-agent", "wNEW:p9", "--source", "specflo-agent-host",
         "--agent", "builder", "--state", "working", "--seq", "1"],
        ["pane", "report-agent", "wNEW:p9", "--source", "specflo-agent-host",
         "--agent", "builder", "--state", "idle", "--seq", "2"],
        ["pane", "release-agent", "wNEW:p9", "--source", "specflo-agent-host",
         "--agent", "builder"],
        ["tab", "close", "wNEW:t9"],
    ]


def test_report_state_rejects_non_herdr_state(fake_herdr):
    adapter = HerdrAdapter()
    with pytest.raises(ValueError):
        adapter.report_state("w1:p1", "a1", "needs-attention")
    assert fake_herdr() == []  # nothing was issued


def test_failed_command_raises_herdr_error(fake_herdr):
    with pytest.raises(HerdrError):
        HerdrAdapter()._run("bogus", "verb")
