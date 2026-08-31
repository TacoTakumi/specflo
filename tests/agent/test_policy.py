"""T-08: dialog auto-answer policy - per-kind answers, danger cancel, flood cap."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from specflo.agent.client import connect
from specflo.agent.host import PiHost
from specflo.agent.policy import DEFAULT_NUDGE, DialogPolicy
from specflo.agent.statefiles import read_status

STUB = Path(__file__).parent / "stub_pi.py"


def wait_until(cond, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def read_events(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").split("\n")
        if line
    ]


def captured(capture: Path) -> list[dict]:
    if not capture.exists():
        return []
    return [
        json.loads(line)
        for line in capture.read_text(encoding="utf-8").split("\n")
        if line
    ]


def ui_responses(capture: Path) -> list[dict]:
    return [f for f in captured(capture) if f.get("type") == "extension_ui_response"]


@pytest.fixture
def make_host(tmp_path):
    hosts = []
    base = tmp_path / "state"

    def make(name: str, scenario: dict, policy: DialogPolicy | None = None):
        capture = tmp_path / f"capture-{name}.jsonl"
        scenario = {**scenario, "capture": str(capture)}
        scenario_file = tmp_path / f"scenario-{name}.json"
        scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
        host = (
            PiHost(
                name,
                [sys.executable, str(STUB), str(scenario_file)],
                cwd=tmp_path,
                base_dir=base,
                policy=policy,
            )
            .start()
            .serve()
        )
        hosts.append(host)
        return host, capture

    yield make, base
    for host in hosts:
        host.close()


def prompt_and_settle(base, name):
    with connect(name, base_dir=base) as client:
        client.send({"type": "prompt", "message": "go"})
        client.read_until(lambda f: f.get("type") == "agent_settled", timeout=10)


def test_each_dialog_kind_gets_policy_conformant_answer(make_host):
    make, base = make_host
    host, capture = make(
        "p1",
        {
            "mode": "dialog",
            "reply": "done",
            "dialogs": [
                {"method": "confirm", "title": "Proceed with the plan?"},
                {"method": "select", "title": "Pick one",
                 "options": ["Abort everything", "Proceed", "Other"]},
                {"method": "input", "title": "Enter a value"},
                {"method": "editor", "title": "Edit the text"},
            ],
        },
    )
    prompt_and_settle(base, "p1")

    responses = ui_responses(capture)
    assert len(responses) == 4
    by_id = {r["id"]: r for r in responses}
    assert by_id["dlg-1"] == {
        "type": "extension_ui_response", "id": "dlg-1", "confirmed": True,
    }
    assert by_id["dlg-2"]["value"] == "Proceed"  # first policy-safe option
    assert by_id["dlg-3"]["value"] == DEFAULT_NUDGE
    assert by_id["dlg-4"]["value"] == DEFAULT_NUDGE

    answers = [e for e in read_events(host.paths.events) if e["type"] == "host_dialog_answer"]
    assert len(answers) == 4
    assert [a["request_id"] for a in answers] == ["dlg-1", "dlg-2", "dlg-3", "dlg-4"]
    assert [a["method"] for a in answers] == ["confirm", "select", "input", "editor"]
    assert answers[0]["answer"] == {"confirmed": True}


def test_danger_matching_dialog_is_cancelled(make_host):
    make, base = make_host
    host, capture = make(
        "p2",
        {
            "mode": "dialog",
            "reply": "done",
            "dialogs": [{"method": "confirm", "title": "Push to the remote?"}],
        },
    )
    prompt_and_settle(base, "p2")
    responses = ui_responses(capture)
    assert responses == [
        {"type": "extension_ui_response", "id": "dlg-1", "cancelled": True}
    ]
    answers = [e for e in read_events(host.paths.events) if e["type"] == "host_dialog_answer"]
    assert answers[0]["answer"] == {"cancelled": True}


def test_select_with_only_danger_options_is_cancelled(make_host):
    make, base = make_host
    host, capture = make(
        "p3",
        {
            "mode": "dialog",
            "reply": "done",
            "dialogs": [{"method": "select", "title": "Pick",
                         "options": ["Delete it", "Deploy now"]}],
        },
    )
    prompt_and_settle(base, "p3")
    assert ui_responses(capture)[0].get("cancelled") is True


def test_fire_and_forget_methods_are_not_answered(make_host):
    make, base = make_host
    host, capture = make(
        "p4",
        {
            "mode": "dialog",
            "reply": "done",
            "dialogs": [{"method": "notify", "message": "heads up"}],
        },
    )
    prompt_and_settle(base, "p4")
    assert ui_responses(capture) == []
    types = [e["type"] for e in read_events(host.paths.events)]
    assert "host_dialog_answer" not in types


def test_flood_flips_needs_attention_and_stops_answering(make_host):
    make, base = make_host
    host, capture = make(
        "p5",
        {
            "mode": "dialog",
            "reply": "never reached",
            "dialogs": [
                {"method": "confirm", "title": f"Question {n}?"} for n in range(1, 6)
            ],
        },
        policy=DialogPolicy(flood_threshold=3),
    )
    with connect("p5", base_dir=base) as client:
        client.send({"type": "prompt", "message": "go"})
        assert wait_until(
            lambda: read_status(host.paths.status)["state"] == "needs-attention"
        )
    # the first three dialogs were answered; the fourth was skipped and the
    # stub stays blocked on it, so no settle and no further answers
    time.sleep(0.3)
    assert len(ui_responses(capture)) == 3
    events = read_events(host.paths.events)
    types = [e["type"] for e in events]
    assert "host_dialog_flood" in types
    assert "host_dialog_skipped" in types
    assert types.count("host_dialog_answer") == 3
    assert read_status(host.paths.status)["state"] == "needs-attention"


def test_dialog_counter_resets_per_run(make_host):
    make, base = make_host
    host, capture = make(
        "p6",
        {
            "mode": "dialog",
            "reply": "done",
            "dialogs": [
                {"method": "confirm", "title": "One?"},
                {"method": "confirm", "title": "Two?"},
            ],
        },
        policy=DialogPolicy(flood_threshold=2),
    )
    prompt_and_settle(base, "p6")
    prompt_and_settle(base, "p6")  # a fresh run resets the counter: no flood
    assert len(ui_responses(capture)) == 4
    types = [e["type"] for e in read_events(host.paths.events)]
    assert "host_dialog_flood" not in types
    # the settle broadcast precedes the status write; poll briefly
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "idle")


def test_auto_answer_disabled_leaves_dialogs_alone(make_host):
    make, base = make_host
    host, capture = make(
        "p7",
        {
            "mode": "dialog",
            "reply": "done",
            "dialogs": [{"method": "confirm", "title": "Proceed?"}],
        },
    )
    host.auto_answer = False
    with connect("p7", base_dir=base) as client:
        client.send({"type": "prompt", "message": "go"})
        request = client.read_until(
            lambda f: f.get("type") == "extension_ui_request", timeout=10
        )
        time.sleep(0.2)
        assert ui_responses(capture) == []  # the host stayed out of it
        client.send(
            {"type": "extension_ui_response", "id": request["id"], "confirmed": True}
        )
        client.read_until(lambda f: f.get("type") == "agent_settled", timeout=10)
    assert len(ui_responses(capture)) == 1
