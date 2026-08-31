"""T-11: herdr wired into the lifecycle - placement, state pushes, degradation."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from specflo.agent.statefiles import ENV_STATE_DIR

STUB = Path(__file__).parent / "stub_pi.py"

# A faithful fake herdr: logs argv, answers canned JSON, and actually
# executes `pane run` commands (detached /bin/sh -c) so a full host
# lifecycle runs behind it.
FAKE_HERDR = '''#!/usr/bin/env python3
import json, os, subprocess, sys

args = sys.argv[1:]
with open(os.environ["FAKE_HERDR_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")

def out(result):
    print(json.dumps({"id": "cli", "result": result}))

if args[:2] == ["workspace", "list"]:
    out({"type": "workspace_list", "workspaces": []})
elif args[:2] == ["workspace", "create"]:
    label = args[args.index("--label") + 1]
    out({"type": "workspace_created",
         "workspace": {"workspace_id": "wF", "label": label}})
elif args[:2] == ["tab", "create"]:
    ws = args[args.index("--workspace") + 1]
    out({"type": "tab_created",
         "tab": {"tab_id": ws + ":t1", "workspace_id": ws},
         "root_pane": {"pane_id": ws + ":p1", "tab_id": ws + ":t1"}})
elif args[:2] == ["pane", "run"]:
    command = args[3]
    subprocess.Popen(["/bin/sh", "-c", command],
                     stdin=subprocess.DEVNULL,
                     stdout=open(os.environ["FAKE_HERDR_PANE_LOG"], "ab"),
                     stderr=subprocess.STDOUT,
                     start_new_session=True)
    out({"type": "ok"})
elif args[:2] in (["pane", "report-agent"], ["pane", "release-agent"],
                  ["tab", "close"]):
    out({"type": "ok"})
else:
    sys.stderr.write("unknown command\\n")
    sys.exit(2)
'''

VENV_BIN = str(Path(sys.executable).parent)


def wait_until(cond, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


class Rig:
    def __init__(self, tmp_path: Path, with_fake_herdr: bool):
        self.tmp_path = tmp_path
        self.base = tmp_path / "state"
        path_parts = [VENV_BIN, "/usr/bin", "/bin"]
        if with_fake_herdr:
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            script = bin_dir / "herdr"
            script.write_text(FAKE_HERDR, encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            path_parts.insert(0, str(bin_dir))
        self.env = {
            **os.environ,
            "PATH": ":".join(path_parts),  # never the real herdr
            ENV_STATE_DIR: str(self.base),
            "FAKE_HERDR_LOG": str(tmp_path / "calls.jsonl"),
            "FAKE_HERDR_PANE_LOG": str(tmp_path / "pane.log"),
        }
        self.started: list[str] = []

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from specflo.cli import main; sys.exit(main())",
                "agent",
                *args,
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=45,
        )

    def start_agent(self, name: str, scenario: dict, *extra: str):
        scenario_file = self.tmp_path / f"scenario-{name}.json"
        scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
        result = self.run_cli(
            "start", name, "--cwd", ".", "--pi-cmd",
            f"{sys.executable} {STUB} {scenario_file}", *extra,
        )
        self.started.append(name)
        return result

    def calls(self) -> list[list[str]]:
        log = Path(self.env["FAKE_HERDR_LOG"])
        if not log.exists():
            return []
        return [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").split("\n")
            if line
        ]

    def cleanup(self):
        if not self.base.is_dir():
            return
        for agent_dir in self.base.iterdir():
            status_file = agent_dir / "status.json"
            if not status_file.is_file():
                continue
            snapshot = json.loads(status_file.read_text())
            for key in ("pi_pid", "host_pid"):
                pid = snapshot.get(key)
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass


@pytest.fixture
def herdr_rig(tmp_path):
    rig = Rig(tmp_path, with_fake_herdr=True)
    yield rig
    rig.cleanup()


@pytest.fixture
def headless_rig(tmp_path):
    rig = Rig(tmp_path, with_fake_herdr=False)
    yield rig
    rig.cleanup()


def reports(calls, state=None):
    rows = [c for c in calls if c[:2] == ["pane", "report-agent"]]
    if state is not None:
        rows = [c for c in rows if c[c.index("--state") + 1] == state]
    return rows


def test_start_places_tab_and_registers_agent(herdr_rig):
    result = herdr_rig.start_agent("a1", {"reply": "ok"})
    assert result.returncode == 0, result.stderr
    assert "pane wF:p1" in result.stdout
    assert "warning" not in result.stderr.lower()

    calls = herdr_rig.calls()
    assert ["workspace", "create", "--label", "agents", "--no-focus"] in calls
    tab_create = next(c for c in calls if c[:2] == ["tab", "create"])
    assert tab_create[tab_create.index("--workspace") + 1] == "wF"
    assert tab_create[tab_create.index("--label") + 1] == "a1"
    assert f"{ENV_STATE_DIR}={herdr_rig.base}" in tab_create
    pane_run = next(c for c in calls if c[:2] == ["pane", "run"])
    assert pane_run[2] == "wF:p1"
    assert pane_run[3].startswith("exec ")
    assert "specflo.agent.cli" in pane_run[3]
    assert "--herdr-pane wF:p1" in pane_run[3]

    # registered on start: the host reported its states into herdr
    assert wait_until(lambda: len(reports(herdr_rig.calls(), "idle")) >= 1)

    # status carries the placement ids (REQ-05)
    probe = json.loads(herdr_rig.run_cli("status", "a1", "--json").stdout)
    assert probe["status"]["herdr_workspace"] == "wF"
    assert probe["status"]["herdr_tab"] == "wF:t1"
    assert probe["status"]["herdr_pane"] == "wF:p1"


def test_prompt_cycle_pushes_working_then_idle(herdr_rig):
    assert herdr_rig.start_agent("a1", {"reply": "ok"}).returncode == 0
    before = len(reports(herdr_rig.calls()))
    result = herdr_rig.run_cli("prompt", "a1", "go")
    assert result.returncode == 0, result.stderr

    assert wait_until(lambda: len(reports(herdr_rig.calls())) >= before + 2)
    new = reports(herdr_rig.calls())[before:]
    states = [c[c.index("--state") + 1] for c in new]
    assert states == ["working", "idle"]
    seqs = [int(c[c.index("--seq") + 1]) for c in new]
    assert seqs == sorted(seqs)


def test_stop_releases_registration(herdr_rig):
    assert herdr_rig.start_agent("a1", {"reply": "ok"}).returncode == 0
    result = herdr_rig.run_cli("stop", "a1")
    assert result.returncode == 0, result.stderr
    releases = [
        c for c in herdr_rig.calls() if c[:2] == ["pane", "release-agent"]
    ]
    assert len(releases) == 1
    assert releases[0][2] == "wF:p1"
    assert releases[0][releases[0].index("--agent") + 1] == "a1"


def test_workspace_override_skips_agent_space(herdr_rig):
    result = herdr_rig.start_agent("a1", {"reply": "ok"}, "--workspace", "w7")
    assert result.returncode == 0, result.stderr
    calls = herdr_rig.calls()
    assert not any(c[:2] == ["workspace", "create"] for c in calls)
    tab_create = next(c for c in calls if c[:2] == ["tab", "create"])
    assert tab_create[tab_create.index("--workspace") + 1] == "w7"


def test_absent_herdr_full_cycle_with_one_warning(headless_rig):
    result = headless_rig.start_agent("a1", {"reply": "degraded ok"})
    assert result.returncode == 0, result.stderr
    warnings = [l for l in result.stderr.split("\n") if "herdr" in l.lower()]
    assert len(warnings) == 1
    assert "warning" in warnings[0].lower()

    prompt = headless_rig.run_cli("prompt", "a1", "go")
    assert prompt.returncode == 0
    assert prompt.stdout == "degraded ok\n"
    assert "herdr" not in prompt.stderr.lower()

    stop = headless_rig.run_cli("stop", "a1")
    assert stop.returncode == 0
    assert "herdr" not in stop.stderr.lower()


def test_no_herdr_flag_degrades_even_with_herdr_present(herdr_rig):
    result = herdr_rig.start_agent("a1", {"reply": "ok"}, "--no-herdr")
    assert result.returncode == 0, result.stderr
    warnings = [l for l in result.stderr.split("\n") if "herdr" in l.lower()]
    assert len(warnings) == 1
    assert herdr_rig.calls() == []  # herdr untouched

    prompt = herdr_rig.run_cli("prompt", "a1", "go")
    assert prompt.returncode == 0
    assert herdr_rig.run_cli("stop", "a1").returncode == 0
    assert herdr_rig.calls() == []
