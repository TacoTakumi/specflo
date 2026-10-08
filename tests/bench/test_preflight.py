"""Run preflight against a stub llama-swap, and the model-id validity of a run."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from modelbench import arms, preflight

ENTRY = "swift15-flash-next-iq4xs-mtp-vision"
OTHER = "swift15-flash-next-iq4xs-strata-2x3090"
FOREIGN = "qwen-27b-3090"


class StubSwap:
    """A llama-swap stand-in on an ephemeral port that records every request."""

    def __init__(self, running: list[str]):
        self.running = list(running)
        self.requests: list[tuple[str, str]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                stub.requests.append((self.command, self.path))

            def do_GET(self) -> None:
                self._record()
                if self.path != "/running":
                    self.send_error(404)
                    return
                body = json.dumps(
                    {"running": [{"model": m, "state": "ready"} for m in stub.running]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                self._record()
                self.send_error(500)

            do_PUT = do_DELETE = do_POST

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "StubSwap":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def config() -> arms.Config:
    return arms.Config(
        entries={ENTRY: {"engine": "llama.cpp"}, OTHER: {"engine": "strata"}},
        harnesses=arms.HARNESSES,
        levels={"quick": {}, "fast": {}, "full": {}},
    )


def _run(config: arms.Config) -> arms.Run:
    return arms.validate_run(config, entry=ENTRY, harness="pi", level="quick", run_index=0)


def _argv(stub: StubSwap, tmp_path, *extra: str) -> list[str]:
    cfg = tmp_path / "arms.yaml"
    cfg.write_text(
        f"entries:\n  {ENTRY}: {{engine: llama.cpp}}\n  {OTHER}: {{engine: strata}}\n"
        "harnesses: [pi, claude-code]\nlevels: {quick: {}, fast: {}, full: {}}\n"
    )
    return [
        "--config", str(cfg), "--base-url", stub.url,
        "--state", str(tmp_path / "loaded.json"), *extra,
    ]


def test_arm_entry_loaded_is_ok(config, tmp_path):
    with StubSwap([ENTRY]) as stub:
        running = preflight.preflight(
            _run(config), base_url=stub.url, state_path=tmp_path / "loaded.json"
        )
    assert running == [ENTRY]
    assert stub.requests == [("GET", "/running")]


def test_nothing_loaded_is_ok(config, tmp_path):
    with StubSwap([]) as stub:
        assert preflight.preflight(
            _run(config), base_url=stub.url, state_path=tmp_path / "loaded.json"
        ) == []
    assert stub.requests == [("GET", "/running")]


def test_foreign_model_is_refused_and_named(config, tmp_path):
    with StubSwap([ENTRY, FOREIGN]) as stub:
        with pytest.raises(preflight.PreflightError) as err:
            preflight.preflight(
                _run(config), base_url=stub.url, state_path=tmp_path / "loaded.json"
            )
    assert err.value.foreign == [FOREIGN]
    assert FOREIGN in str(err.value)
    assert stub.requests == [("GET", "/running")]


def test_model_the_bench_loaded_is_not_foreign(config, tmp_path):
    state = tmp_path / "loaded.json"
    preflight.record_bench_load(state, OTHER)
    assert preflight.read_bench_loaded(state) == {OTHER}
    with StubSwap([OTHER]) as stub:
        assert preflight.preflight(_run(config), base_url=stub.url, state_path=state) == [
            OTHER
        ]


def test_main_foreign_exits_nonzero_sends_only_get_running(tmp_path, capsys):
    with StubSwap([FOREIGN, "other-foreign"]) as stub:
        before = list(stub.running)
        rc = preflight.main(
            _argv(stub, tmp_path, "--entry", ENTRY, "--harness", "pi",
                  "--level", "quick", "--run-index", "0")
        )
        after = list(stub.running)
    assert rc != 0
    err = capsys.readouterr().err
    assert FOREIGN in err and "other-foreign" in err
    assert stub.requests == [("GET", "/running")]
    assert after == before


def test_main_ok_exits_zero(tmp_path):
    with StubSwap([ENTRY]) as stub:
        rc = preflight.main(
            _argv(stub, tmp_path, "--entry", ENTRY, "--harness", "claude-code",
                  "--level", "full", "--run-index", "3")
        )
    assert rc == 0
    assert stub.requests == [("GET", "/running")]


def test_main_bad_run_field_sends_nothing(tmp_path, capsys):
    with StubSwap([ENTRY]) as stub:
        rc = preflight.main(
            _argv(stub, tmp_path, "--entry", "no-such-entry", "--harness", "pi",
                  "--level", "quick", "--run-index", "0")
        )
    assert rc != 0
    assert "entry" in capsys.readouterr().err
    assert stub.requests == []


def test_unreachable_llama_swap_is_refused(config, tmp_path):
    with StubSwap([]) as stub:
        url = stub.url
    with pytest.raises(preflight.PreflightError):
        preflight.preflight(_run(config), base_url=url, state_path=tmp_path / "x.json")


def _log(*models: str | None) -> dict:
    reqs = []
    for i, m in enumerate(models):
        r = {"id": f"r{i}"}
        if m is not None:
            r["model"] = m
        reqs.append(r)
    return {"requests": reqs}


def test_model_ids_from_log_requests():
    assert preflight.request_model_ids(_log(ENTRY, ENTRY, None)) == {ENTRY}


def test_single_entry_run_is_valid():
    result = preflight.model_validity(ENTRY, log=_log(ENTRY, ENTRY))
    assert result == {"valid": True, "model_ids": [ENTRY]}


def test_second_model_id_marks_run_invalid():
    result = preflight.model_validity(ENTRY, log=_log(ENTRY, OTHER, ENTRY))
    assert result["valid"] is False
    assert result["model_ids"] == sorted([ENTRY, OTHER])


def test_explicit_id_list_is_accepted():
    assert preflight.model_validity(ENTRY, ids=[ENTRY])["valid"] is True
    bad = preflight.model_validity(ENTRY, ids=[ENTRY, FOREIGN])
    assert bad == {"valid": False, "model_ids": sorted([ENTRY, FOREIGN])}


def test_no_recorded_model_ids_is_invalid():
    assert preflight.model_validity(ENTRY, log=_log(None)) == {
        "valid": False,
        "model_ids": [],
    }
