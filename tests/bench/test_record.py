"""Run-record schema, arm config and the mb.py dispatcher."""

import copy
import subprocess
import sys
import types
from pathlib import Path

import pytest

from modelbench import arms, record

BENCH = Path(__file__).resolve().parents[2] / "bench"
ENTRY = "swift15-flash-next-iq4xs-mtp-vision"


def valid_record() -> dict:
    return {
        "arm": {"entry": ENTRY, "harness": "pi"},
        "level": "quick",
        "run_index": 0,
        "versions": {
            "harness": "0.70.0",
            "engine_build": "llamacpp-qwen4exp-a67",
            "specflo": "0.16.0",
            "entry": ENTRY,
            "gguf_path": "/models/swift.gguf",
        },
        "settings": {
            "sampling": {"temperature": 0.6, "top_p": 0.95},
            "context_window": 262144,
            "autonomy": "auto",
            "pass_cap": 3,
        },
        "started_at": "2026-10-07T10:00:00+00:00",
        "ended_at": "2026-10-07T10:20:00+00:00",
        "end_reason": "complete",
        "valid": True,
        "score": 0.75,
        "metrics": {},
        "diagnostics": {},
        "telemetry": {"available": False},
        "logs": {"session": "/runs/x/session.jsonl"},
    }


def test_valid_record_passes():
    assert record.validate_record(valid_record()) == []
    record.check_record(valid_record())


def _paths(fields: dict, prefix: str = "") -> list[str]:
    out = []
    for name, spec in fields.items():
        path = f"{prefix}{name}"
        out.append(path)
        if isinstance(spec, dict):
            out.extend(_paths(spec, path + "."))
    return out


@pytest.mark.parametrize("path", _paths(record.REQUIRED_FIELDS))
def test_missing_required_field_fails_naming_it(path):
    rec = copy.deepcopy(valid_record())
    *parents, leaf = path.split(".")
    node = rec
    for p in parents:
        node = node[p]
    del node[leaf]
    errors = record.validate_record(rec)
    assert errors and any(e.startswith(f"{path}:") for e in errors), errors
    with pytest.raises(record.RecordError, match=path.replace(".", r"\.")):
        record.check_record(rec)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("run_index", "0"),
        ("run_index", True),
        ("valid", 1),
        ("score", "high"),
        ("metrics", []),
        ("end_reason", "crashed"),
        ("arm.harness", "codex"),
        ("level", "medium"),
        ("settings.pass_cap", 2.5),
    ],
)
def test_wrong_type_or_value_fails_naming_field(path, value):
    rec = valid_record()
    *parents, leaf = path.split(".")
    node = rec
    for p in parents:
        node = node[p]
    node[leaf] = value
    errors = record.validate_record(rec)
    assert any(e.startswith(f"{path}:") for e in errors), errors


def test_non_dict_record_fails():
    assert record.validate_record([]) != []


# --- arm config ---------------------------------------------------------


@pytest.fixture
def config() -> arms.Config:
    return arms.load_config(BENCH / "arms.yaml")


def test_shipped_config_knows_both_entries(config):
    assert ENTRY in config.entries
    assert "swift15-flash-next-iq4xs-strata-2x3090" in config.entries
    assert set(config.harnesses) == {"pi", "claude-code"}
    assert set(config.levels) == {"quick", "fast", "full"}


def test_valid_run_carries_all_four_fields(config):
    run = arms.validate_run(config, entry=ENTRY, harness="claude-code", level="full", run_index=2)
    assert (run.entry, run.harness, run.level, run.run_index) == (ENTRY, "claude-code", "full", 2)


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("entry", {"entry": "no-such-entry"}),
        ("entry", {"entry": None}),
        ("harness", {"harness": "codex"}),
        ("harness", {"harness": ""}),
        ("level", {"level": "medium"}),
        ("level", {"level": None}),
        ("run_index", {"run_index": -1}),
        ("run_index", {"run_index": None}),
    ],
)
def test_bad_run_field_is_rejected_naming_it(config, field, kwargs):
    args = {"entry": ENTRY, "harness": "pi", "level": "quick", "run_index": 0} | kwargs
    with pytest.raises(arms.ArmError) as exc:
        arms.validate_run(config, **args)
    assert exc.value.field == field
    assert str(exc.value).startswith(f"{field}:")


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("harnesses", "entries: {a: {engine: llama.cpp}}\nharnesses: [pi, codex]\nlevels: {quick: {}}\n"),
        ("levels", "entries: {a: {engine: llama.cpp}}\nharnesses: [pi]\nlevels: {medium: {}}\n"),
        ("entries", "entries: {}\nharnesses: [pi]\nlevels: {quick: {}}\n"),
        ("entries.a.engine", "entries: {a: {}}\nharnesses: [pi]\nlevels: {quick: {}}\n"),
        ("harnesses", "entries: {a: {engine: llama.cpp}}\nlevels: {quick: {}}\n"),
    ],
)
def test_bad_config_is_rejected_naming_field(tmp_path, field, text):
    path = tmp_path / "arms.yaml"
    path.write_text(text)
    with pytest.raises(arms.ArmError) as exc:
        arms.load_config(path)
    assert exc.value.field == field


# --- mb.py dispatcher ---------------------------------------------------


def test_dispatcher_calls_the_command_main(monkeypatch):
    import mb

    seen = []
    fake = types.ModuleType("modelbench.cmd_fake")
    fake.main = lambda argv: seen.append(argv) or 7
    monkeypatch.setitem(sys.modules, "modelbench.cmd_fake", fake)
    assert mb.main(["fake", "--x", "1"]) == 7
    assert seen == [["--x", "1"]]


def test_dispatcher_rejects_unknown_command(capsys):
    import mb

    assert mb.main(["no_such_cmd"]) != 0
    assert "no_such_cmd" in capsys.readouterr().err


def test_dispatcher_script_exits_nonzero_on_unknown_and_empty():
    for argv in (["no_such_cmd"], []):
        proc = subprocess.run(
            [sys.executable, str(BENCH / "mb.py"), *argv], capture_output=True, text=True
        )
        assert proc.returncode != 0
        assert proc.stderr.strip()
