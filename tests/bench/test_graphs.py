"""Comparison check and graphs with disclosures, from synthetic run records."""

import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")

from modelbench import cmd_graph, graphs  # noqa: E402

ENTRY_A = "swift15-flash-next-iq4xs-mtp-vision"
ENTRY_B = "swift15-flash-next-iq4xs-strata-2x3090"
ENGINES = {ENTRY_A: "llama.cpp", ENTRY_B: "strata"}


def make_record(
    entry: str = ENTRY_A,
    harness: str = "pi",
    level: str = "quick",
    run_index: int = 0,
    score: float = 0.5,
    **overrides,
) -> dict:
    rec = {
        "arm": {"entry": entry, "harness": harness},
        "level": level,
        "run_index": run_index,
        "versions": {
            "harness": "0.70.0" if harness == "pi" else "2.1.0",
            "engine_build": f"build-{entry[-6:]}",
            "specflo": "0.16.0",
            "entry": entry,
            "gguf_path": f"/models/{entry}.gguf",
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
        "score": score,
        "metrics": {
            "turns": 10 + run_index,
            "output_tokens_per_turn": {"mean": 200.0, "median": 180.0, "max": 900},
            "tool_errors": run_index,
            "wall_time_s": 1200.0,
        },
        "diagnostics": {},
        "telemetry": {"available": False},
        "logs": {"session": "/runs/x/session.jsonl"},
    }
    for path, value in overrides.items():
        *parents, leaf = path.split("__")
        node = rec
        for p in parents:
            node = node[p]
        node[leaf] = value
    return rec


def write_records(root: Path, records: list[dict]) -> Path:
    for i, rec in enumerate(records):
        run_dir = root / f"run-{i}"
        run_dir.mkdir(parents=True)
        (run_dir / "record.json").write_text(json.dumps(rec))
    return root


def harness_pair(level: str = "quick", runs: int = 3) -> list[dict]:
    out = []
    for h in ("pi", "claude-code"):
        out += [make_record(harness=h, level=level, run_index=i, score=0.2 * i) for i in range(runs)]
    return out


def arm_values(rec: dict, engine: str) -> list[str]:
    vals = [rec["arm"]["entry"], rec["arm"]["harness"], engine]
    vals += [str(v) for k, v in rec["versions"].items()]
    for k, v in rec["settings"].items():
        if isinstance(v, dict):
            vals += [f"{sk}={sv}" for sk, sv in v.items()]
        else:
            vals.append(str(v))
    return vals


# -- comparison check --------------------------------------------------------


def test_check_accepts_one_variable_harness():
    arms = graphs.summarise_arms(harness_pair(), ENGINES)
    assert graphs.check_comparison(arms) == "harness"


def test_check_accepts_one_variable_entry():
    recs = [make_record(entry=e, run_index=i) for e in (ENTRY_A, ENTRY_B) for i in range(2)]
    assert graphs.check_comparison(graphs.summarise_arms(recs, ENGINES)) == "entry"


def test_check_refuses_two_variables_naming_fields():
    recs = [make_record(ENTRY_A, "pi"), make_record(ENTRY_B, "claude-code")]
    with pytest.raises(graphs.ComparisonError) as exc:
        graphs.check_comparison(graphs.summarise_arms(recs, ENGINES))
    for field in ("arm.entry", "arm.harness", "versions.harness"):
        assert field in exc.value.fields
        assert field in str(exc.value)


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"settings__pass_cap": 5}, "settings.pass_cap"),
        ({"settings__context_window": 131072}, "settings.context_window"),
        ({"settings__autonomy": "step"}, "settings.autonomy"),
        ({"settings__sampling": {"temperature": 1.0, "top_p": 0.95}}, "settings.sampling"),
        ({"versions__specflo": "0.15.0"}, "versions.specflo"),
    ],
)
def test_check_refuses_a_differing_setting(override, field):
    recs = [make_record(harness="pi"), make_record(harness="claude-code", **override)]
    with pytest.raises(graphs.ComparisonError) as exc:
        graphs.check_comparison(graphs.summarise_arms(recs, ENGINES))
    assert exc.value.fields == [field]


def test_check_refuses_harness_compare_with_differing_engine_build():
    recs = [make_record(harness="pi"), make_record(harness="claude-code", versions__engine_build="other")]
    with pytest.raises(graphs.ComparisonError) as exc:
        graphs.check_comparison(graphs.summarise_arms(recs, ENGINES))
    assert "versions.engine_build" in exc.value.fields


def test_check_refuses_a_field_that_varies_within_an_arm():
    recs = [make_record(run_index=0), make_record(run_index=1, settings__pass_cap=9)]
    with pytest.raises(graphs.ComparisonError, match="settings.pass_cap"):
        graphs.summarise_arms(recs, ENGINES)


def test_excluded_runs_drop_out():
    recs = [
        make_record(run_index=0),
        make_record(run_index=1, valid=False),
        make_record(run_index=2, diagnostics={"contaminated": True}),
        # An excluded run's differing setting does not refuse the comparison.
        make_record(harness="claude-code", run_index=0),
        make_record(harness="claude-code", run_index=1, valid=False, settings__pass_cap=7),
    ]
    included, excluded = graphs.split_runs(recs)
    assert len(included) == 2 and len(excluded) == 3
    arms = graphs.summarise_arms(included, ENGINES)
    assert [a.run_count for a in arms] == [1, 1]
    assert graphs.check_comparison(arms) == "harness"


# -- graphs ------------------------------------------------------------------


def test_graph_writes_images_and_disclosures(tmp_path):
    recs = harness_pair("quick") + harness_pair("fast", runs=2)
    recs.append(make_record(level="quick", run_index=9, valid=False))
    rec_dir = write_records(tmp_path / "records", recs)
    out = tmp_path / "out"
    rc = cmd_graph.main(["--records", str(rec_dir), "--out", str(out)])
    assert rc == 0
    for level, runs in (("quick", 3), ("fast", 2)):
        pngs = sorted(out.glob(f"{level}-*.png"))
        names = [p.name for p in pngs]
        assert any(n.endswith("-pass-rate.png") for n in names), names
        assert any(n.endswith("-metrics.png") for n in names), names
        for png in pngs:
            assert png.stat().st_size > 0
            text = png.with_suffix(".txt").read_text()
            for rec in (r for r in recs if r["level"] == level and r["valid"]):
                for value in arm_values(rec, ENGINES[rec["arm"]["entry"]]):
                    assert value in text, (value, png.name)
            assert text.count(f"runs={runs}") == 2, text
    quick_text = next(out.glob("quick-*-pass-rate.txt")).read_text()
    assert "excluded runs: 1" in quick_text


def test_graph_api_returns_disclosures(tmp_path):
    written = graphs.generate(harness_pair(), tmp_path, ENGINES)
    assert written
    for image, disclosure in written:
        assert image.exists() and image.stat().st_size > 0
        assert "runs=3" in disclosure
        assert image.with_suffix(".txt").read_text() == disclosure


def test_graph_of_two_variable_arms_exits_nonzero(tmp_path, capsys):
    recs = [make_record(ENTRY_A, "pi"), make_record(ENTRY_B, "claude-code")]
    rec_dir = write_records(tmp_path / "records", recs)
    out = tmp_path / "out"
    rc = cmd_graph.main(["--records", str(rec_dir), "--out", str(out)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "arm.entry" in err and "arm.harness" in err
    assert not out.exists() or not any(out.iterdir())


def test_graph_of_arms_differing_in_a_setting_exits_nonzero(tmp_path, capsys):
    recs = [make_record(harness="pi"), make_record(harness="claude-code", settings__pass_cap=5)]
    rec_dir = write_records(tmp_path / "records", recs)
    rc = cmd_graph.main(["--records", str(rec_dir), "--out", str(tmp_path / "out")])
    assert rc != 0
    assert "settings.pass_cap" in capsys.readouterr().err


def test_arm_option_selects_one_comparison(tmp_path):
    recs = [make_record(e, h, run_index=i) for e in (ENTRY_A, ENTRY_B) for h in ("pi", "claude-code") for i in range(2)]
    rec_dir = write_records(tmp_path / "records", recs)
    out = tmp_path / "out"
    # All four arms together differ in two variables.
    assert cmd_graph.main(["--records", str(rec_dir), "--out", str(out)]) != 0
    rc = cmd_graph.main([
        "--records", str(rec_dir), "--out", str(out),
        "--arm", f"{ENTRY_A}:pi", "--arm", f"{ENTRY_A}:claude-code",
    ])
    assert rc == 0
    text = next(out.glob("quick-*-pass-rate.txt")).read_text()
    assert ENTRY_A in text and ENTRY_B not in text


def test_schema_invalid_record_exits_nonzero(tmp_path, capsys):
    bad = make_record()
    del bad["settings"]["pass_cap"]
    rec_dir = write_records(tmp_path / "records", [bad])
    rc = cmd_graph.main(["--records", str(rec_dir), "--out", str(tmp_path / "out")])
    assert rc != 0
    assert "settings.pass_cap" in capsys.readouterr().err


def test_mb_dispatches_graph(tmp_path):
    import subprocess
    import sys

    bench = Path(__file__).resolve().parents[2] / "bench"
    rec_dir = write_records(tmp_path / "records", copy.deepcopy(harness_pair()))
    proc = subprocess.run(
        [sys.executable, str(bench / "mb.py"), "graph", "--records", str(rec_dir), "--out", str(tmp_path / "out")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert list((tmp_path / "out").glob("*.png"))
