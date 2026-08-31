"""T-13: structural guards - pipeline independence (REQ-15), content-agnostic host (REQ-16)."""

from __future__ import annotations

import ast
import json
import sys
import time
from pathlib import Path

import pytest

from specflo.agent.client import connect
from specflo.agent.host import PiHost

SRC = Path(__file__).resolve().parents[2] / "src" / "specflo"
AGENT_DIR = SRC / "agent"
STUB = Path(__file__).parent / "stub_pi.py"

# The composition point: the one pipeline-side file allowed to import the
# agent subsystem (it only registers the typer group and bridges config).
COMPOSITION_FILES = {SRC / "cli.py"}

# Controller-side sentinel conventions the HOST must never know about.
SENTINEL_LITERALS = ("QUESTION:", "BLOCKED:", "Completed project")


def imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # the package a relative import resolves against: the file's directory
    package_parts = ["specflo", *path.relative_to(SRC).parent.parts]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package_parts[: len(package_parts) - (node.level - 1)]
                module = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            names.add(module)
            for alias in node.names:
                names.add(f"{module}.{alias.name}")
    return names


def specflo_modules(path: Path) -> set[str]:
    return {
        n for n in imported_names(path) if n == "specflo" or n.startswith("specflo.")
    }


def test_agent_modules_import_no_pipeline_code():
    agent_files = sorted(AGENT_DIR.glob("*.py"))
    assert agent_files, "agent subsystem sources not found"
    for path in agent_files:
        offending = {
            n
            for n in specflo_modules(path)
            if not n.startswith("specflo.agent") and n != "specflo"
        }
        assert not offending, f"{path.name} imports pipeline code: {sorted(offending)}"


def test_pipeline_modules_import_no_agent_code():
    pipeline_files = [
        p
        for p in SRC.rglob("*.py")
        if AGENT_DIR not in p.parents and p not in COMPOSITION_FILES
    ]
    assert pipeline_files
    for path in pipeline_files:
        offending = {
            n for n in specflo_modules(path) if n.startswith("specflo.agent")
        }
        assert not offending, f"{path} imports the agent subsystem: {sorted(offending)}"


def test_composition_point_is_the_only_bridge():
    # cli.py may import the agent group - and only the cli surface of it
    names = specflo_modules(SRC / "cli.py")
    agent_imports = {n for n in names if n.startswith("specflo.agent")}
    assert agent_imports  # the group is actually registered
    allowed = {"specflo.agent"}
    assert all(
        n in allowed or n.startswith("specflo.agent.cli") for n in agent_imports
    ), sorted(agent_imports)


def test_agent_sources_carry_no_sentinel_literals():
    for path in sorted(AGENT_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for literal in SENTINEL_LITERALS:
            assert literal not in source, f"{path.name} contains {literal!r}"


# -- verbatim flow: sentinel-looking text changes nothing -------------------


def wait_until(cond, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def test_sentinel_looking_reply_flows_verbatim(tmp_path):
    reply = "QUESTION: verbatim? BLOCKED: no. Completed project."
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(json.dumps({"reply": reply}), encoding="utf-8")
    host = (
        PiHost(
            "s1",
            [sys.executable, str(STUB), str(scenario_file)],
            cwd=tmp_path,
            base_dir=tmp_path / "state",
        )
        .start()
        .serve()
    )
    try:
        with connect("s1", base_dir=tmp_path / "state") as client:
            client.send({"type": "prompt", "message": "go"})
            client.read_until(lambda f: f.get("type") == "agent_settled", timeout=10)
            response = client.request({"type": "get_last_assistant_text"}, timeout=10)
        assert response["data"]["text"] == reply  # verbatim, uninterpreted
        assert host.state == "idle"  # no sentinel-driven state change
        assert reply in host.paths.events.read_text(encoding="utf-8")
        types = [
            json.loads(line)["type"]
            for line in host.paths.events.read_text(encoding="utf-8").split("\n")
            if line
        ]
        assert "host_error" not in types
    finally:
        host.close()
