"""The reasoning-setting probe: per-engine derivation, on/off from the reply, the refusal, the CLI.

The offline tests use no model. The real-harness tests run the installed pi
and claude binaries against a stub endpoint in this test, through the capture
proxy, so they check the request each harness really sends. The rig test runs
the probe against llama-swap and is run only with `-m rig`.
"""

from __future__ import annotations

import ast
import json
import shutil
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from modelbench import arms, cmd_probe, preflight, probe

LLAMA = "swift15-flash-next-iq4xs-mtp-vision"
STRATA = "swift15-flash-next-iq4xs-strata-2x3090"
HAVE_PI = shutil.which("pi") is not None
HAVE_CLAUDE = (Path.home() / ".local" / "bin" / "claude").exists()

# The reasoning fields Claude Code sends for a model id it does not know, and
# what pi sends from the frozen models.json at its default level (high).
CC_BODY = {"model": LLAMA, "thinking": {"type": "adaptive", "display": "updates"},
           "output_config": {"effort": "high"}, "max_tokens": 32000, "messages": []}
PI_BODY = {"model": LLAMA, "messages": [],
           "chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True,
                                    "reasoning_effort": "xhigh"}}


# -- llama.cpp ---------------------------------------------------------------------


def test_llama_cpp_drops_claude_codes_effort_and_adaptive_thinking():
    got = probe.llama_cpp_receives(CC_BODY, "anthropic")
    assert got.thinking is True
    assert got.effort == "xhigh"
    assert got.source == "template default"
    assert got.ignored == ("thinking.type=adaptive", "output_config.effort=high")


def test_llama_cpp_ignores_a_low_effort_and_a_disabled_thinking_on_messages():
    low = probe.llama_cpp_receives(dict(CC_BODY, output_config={"effort": "low"}), "anthropic")
    assert (low.thinking, low.effort) == (True, "xhigh")
    off = probe.llama_cpp_receives({"thinking": {"type": "disabled"}}, "anthropic")
    assert (off.thinking, off.effort) == (True, "xhigh")


def test_llama_cpp_messages_honours_chat_template_kwargs_and_an_enabled_budget():
    body = dict(CC_BODY, chat_template_kwargs={"reasoning_effort": "low"})
    assert probe.llama_cpp_receives(body, "anthropic").effort == "low"
    body = {"chat_template_kwargs": {"enable_thinking": False}}
    assert probe.llama_cpp_receives(body, "anthropic").thinking is False
    got = probe.llama_cpp_receives({"thinking": {"type": "enabled", "budget_tokens": 4096}}, "anthropic")
    assert (got.thinking, got.budget, got.ignored) == (True, 4096, ())


def test_llama_cpp_chat_completions_reads_pis_template_kwargs():
    got = probe.llama_cpp_receives(PI_BODY, "openai")
    assert (got.thinking, got.effort) == (True, "xhigh")
    assert got.source == "chat_template_kwargs.reasoning_effort"


def test_llama_cpp_chat_completions_off_and_top_level_effort():
    off = probe.llama_cpp_receives({"chat_template_kwargs": {"enable_thinking": False}}, "openai")
    assert (off.thinking, off.effort) == (False, None)
    none = probe.llama_cpp_receives(dict(PI_BODY, reasoning_effort="none"), "openai")
    assert (none.thinking, none.effort) == (False, None)
    medium = probe.llama_cpp_receives(dict(PI_BODY, reasoning_effort="medium"), "openai")
    assert (medium.effort, medium.source) == ("medium", "reasoning_effort")
    high = probe.llama_cpp_receives({"chat_template_kwargs": {"reasoning_effort": "high"}}, "openai")
    assert high.effort == "xhigh"


# -- Strata -------------------------------------------------------------------------


def test_strata_honours_claude_codes_effort():
    got = probe.strata_receives(dict(CC_BODY, model=STRATA), "anthropic")
    assert (got.thinking, got.effort, got.source) == (True, "xhigh", "output_config.effort")
    low = probe.strata_receives(dict(CC_BODY, output_config={"effort": "low"}), "anthropic")
    assert low.effort == "low"


def test_strata_messages_disabled_budget_and_on_request():
    off = probe.strata_receives({"thinking": {"type": "disabled"}, "output_config": {"effort": "high"}},
                                "anthropic")
    assert (off.thinking, off.effort) == (False, None)
    budget = probe.strata_receives({"thinking": {"type": "enabled", "budget_tokens": 3000}}, "anthropic")
    assert budget.effort == "medium"
    assert probe.strata_receives({}, "anthropic").thinking is True
    assert probe.strata_receives({}, "anthropic", think_unasked=False).thinking is False


def test_strata_chat_completions_reads_pis_template_kwargs():
    got = probe.strata_receives(dict(PI_BODY, model=STRATA), "openai")
    assert (got.thinking, got.effort) == (True, "xhigh")
    off = probe.strata_receives({"chat_template_kwargs": {"enable_thinking": False,
                                                          "reasoning_effort": "low"}}, "openai")
    assert off.thinking is False
    assert probe.strata_receives({"reasoning_effort": "high"}, "openai").effort == "xhigh"
    assert probe.strata_receives({"reasoning": {"effort": "low"}}, "openai").effort == "low"


def test_strata_shared_settings_fill_in_only_where_the_request_names_no_level():
    shared = {"reasoning_effort": "low"}
    filled = probe.strata_receives({"messages": []}, "openai", shared=shared)
    assert filled.effort == "low" and "shared" in filled.source
    assert probe.strata_receives(PI_BODY, "openai", shared=shared).effort == "xhigh"
    assert probe.strata_receives(CC_BODY, "anthropic", shared=shared).effort == "xhigh"
    off = probe.strata_receives({}, "anthropic", shared={"reasoning_effort": "none"})
    assert off.thinking is False


def test_strata_refuses_an_unknown_effort():
    with pytest.raises(probe.ProbeError, match="400"):
        probe.strata_receives({"reasoning_effort": "huge"}, "openai")


def test_engine_receives_dispatches_and_refuses_an_unknown_engine():
    assert probe.engine_receives("llama.cpp", CC_BODY, "anthropic").source == "template default"
    assert probe.engine_receives("strata", CC_BODY, "anthropic").source == "output_config.effort"
    with pytest.raises(ValueError):
        probe.engine_receives("vllm", CC_BODY, "anthropic")


def test_sent_fields_keep_only_reasoning_fields():
    assert probe.sent_fields(CC_BODY, "anthropic") == {
        "thinking": CC_BODY["thinking"], "output_config": {"effort": "high"}}
    assert probe.sent_fields(PI_BODY, "openai") == {"chat_template_kwargs": PI_BODY["chat_template_kwargs"]}


STRATA_FRONTEND = Path("/vol2/LLM/strata-v01403/serve/frontend.py")
TEMPLATES = (Path.home() / "AI/Engines/chat-templates/Qwen-Qwen3.8-Flash-Next-unsloth.jinja",
             Path("/vol2/LLM/strata-packs/swift15-iq4xs/tokenizer/chat_template.jinja"))


@pytest.mark.skipif(not STRATA_FRONTEND.exists(), reason="the Strata source is not on this machine")
def test_strata_effort_table_matches_the_engine_source():
    tree = ast.parse(STRATA_FRONTEND.read_text())
    table = next(ast.literal_eval(node.value) for node in tree.body
                 if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "EFFORT")
    assert table == probe.STRATA_EFFORT


@pytest.mark.skipif(not all(p.exists() for p in TEMPLATES), reason="the chat templates are not here")
def test_both_entries_templates_default_to_xhigh_and_read_high_as_xhigh():
    for path in TEMPLATES:
        assert "reasoning_effort|default('xhigh')" in path.read_text(), path
    # llama-server passes "high" to the template, which reads it as xhigh;
    # Strata maps it in its own table first.
    assert "resolved_reasoning_effort == 'high'" in TEMPLATES[0].read_text()
    assert probe.STRATA_EFFORT["high"] == "xhigh"
    assert probe.template_effort(None) == "xhigh"
    assert probe.template_effort("high") == "xhigh"
    assert probe.template_effort("low") == "low"


# -- reasoning in the reply -----------------------------------------------------------


def _sse(*payloads: dict) -> bytes:
    return "".join(f"data: {json.dumps(p)}\n\n" for p in payloads).encode() + b"data: [DONE]\n\n"


def _anthropic_sse(thinking: str, text: str = "391") -> bytes:
    events = [
        {"type": "message_start", "message": {"id": "m", "content": []}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": thinking}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def test_openai_stream_reasoning_on_and_off():
    on = _sse({"choices": [{"delta": {"reasoning_content": "17*23 = 391"}}]},
              {"choices": [{"delta": {"content": "391"}}]})
    assert probe.response_reasoning("openai", on) == len("17*23 = 391")
    off = _sse({"choices": [{"delta": {"content": "391"}}]})
    assert probe.response_reasoning("openai", off) == 0


def test_openai_plain_json_and_error_replies():
    body = json.dumps({"choices": [{"message": {"reasoning_content": "hm", "content": "391"}}]}).encode()
    assert probe.response_reasoning("openai", body) == 2
    assert probe.response_reasoning("openai", b'{"error": {"message": "boom"}}') is None
    assert probe.response_reasoning("openai", b"") is None


def test_anthropic_stream_reasoning_on_and_off():
    assert probe.response_reasoning("anthropic", _anthropic_sse("think")) == 5
    assert probe.response_reasoning("anthropic", _anthropic_sse("  ")) == 0
    message = {"type": "message", "content": [{"type": "thinking", "thinking": "abc"},
                                              {"type": "text", "text": "391"}]}
    assert probe.response_reasoning("anthropic", json.dumps(message).encode()) == 3
    plain = {"type": "message", "content": [{"type": "text", "text": "391"}]}
    assert probe.response_reasoning("anthropic", json.dumps(plain).encode()) == 0


# -- comparison and refusal -------------------------------------------------------------


def result(harness="claude-code", entry=LLAMA, engine="llama.cpp", *, thinking=True,
           effort="xhigh", chars=100, budget=None) -> probe.ProbeResult:
    return probe.ProbeResult(
        harness=harness, entry=entry, engine=engine, api=probe.API_OF_HARNESS[harness],
        sent={}, received=probe.Received(thinking=thinking, effort=effort if thinking else None,
                                         source="test", budget=budget),
        reasoning_chars=chars,
    )


def test_matching_probes_may_be_compared():
    a, b = result(), result(entry=STRATA, engine="strata")
    assert probe.differences(a, b) == []
    assert probe.refusal(a, b) is None
    probe.require_same(a, b)


def test_a_different_effort_is_refused():
    a, b = result(), result(entry=STRATA, engine="strata", effort="medium")
    message = probe.refusal(a, b)
    assert message and "refusing to compare" in message and "effort" in message
    with pytest.raises(probe.ProbeRefusal) as exc:
        probe.require_same(a, b)
    assert exc.value.differences == [f"effort: {LLAMA} (llama.cpp) xhigh, {STRATA} (strata) medium"]


def test_reasoning_on_against_off_is_refused():
    a, b = result(), result(entry=STRATA, engine="strata", thinking=False, chars=0)
    found = probe.differences(a, b)
    assert any(d.startswith("reasoning:") for d in found)
    assert any(d.startswith("effort:") for d in found)


def test_a_different_budget_is_refused():
    a, b = result(budget=4096), result(entry=STRATA, engine="strata")
    assert probe.differences(a, b) == [f"thinking budget: {LLAMA} (llama.cpp) 4096, {STRATA} (strata) None"]


def test_a_reply_that_contradicts_the_derivation_is_refused():
    a, b = result(chars=0), result(entry=STRATA, engine="strata")
    assert probe.differences(a, b) == [
        f"{LLAMA} (llama.cpp): the reply shows reasoning off, the request rules give on"]


def test_a_probe_with_no_readable_reply_is_refused():
    a, b = result(chars=None), result(entry=STRATA, engine="strata")
    assert "no readable reply" in probe.differences(a, b)[0]


def test_probes_of_different_harnesses_are_not_compared():
    with pytest.raises(ValueError, match="different harnesses"):
        probe.differences(result(harness="pi"), result(entry=STRATA, engine="strata"))


def test_result_round_trips_through_json():
    original = probe.ProbeResult(
        harness="claude-code", entry=LLAMA, engine="llama.cpp", api="anthropic",
        sent=probe.sent_fields(CC_BODY, "anthropic"),
        received=probe.llama_cpp_receives(CC_BODY, "anthropic"), reasoning_chars=12, requests=3,
    )
    data = json.loads(json.dumps(original.to_dict()))
    assert data["observed"] is True
    assert data["received"]["ignored"] == ["thinking.type=adaptive", "output_config.effort=high"]
    assert probe.ProbeResult.from_dict(data) == original


# -- capture proxy and request choice ----------------------------------------------------


class Stub:
    """A model endpoint for both APIs: replies stream with `reasoning` as the
    reasoning (none when empty) and "391" as the answer."""

    def __init__(self, reasoning: str = "17 times 23 is 391.") -> None:
        self.reasoning = reasoning
        self.requests: list[tuple[str, dict]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.end_headers()

            def do_GET(self):
                self._send(200, "application/json", b"{}")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                stub.requests.append((self.path, body))
                if "count_tokens" in self.path:
                    return self._send(200, "application/json", b'{"input_tokens": 100}')
                if self.path.split("?")[0].endswith("/chat/completions"):
                    return self._send(200, "text/event-stream", stub.openai(body))
                if self.path.startswith("/v1/messages"):
                    return self._send(200, "text/event-stream", stub.anthropic(body))
                self._send(404, "text/plain", b"")

            def _send(self, status: int, kind: str, data: bytes) -> None:
                self.send_response(status)
                self.send_header("content-type", kind)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def openai(self, body: dict) -> bytes:
        def chunk(delta: dict, finish: str | None = None) -> dict:
            return {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": body.get("model"),
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        parts = [chunk({"role": "assistant", "content": ""})]
        if self.reasoning:
            parts.append(chunk({"reasoning_content": self.reasoning}))
        parts += [chunk({"content": "391"}), chunk({}, "stop")]
        return _sse(*parts)

    def anthropic(self, body: dict) -> bytes:
        message = {"id": "msg_1", "type": "message", "role": "assistant", "model": body.get("model"),
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 10, "output_tokens": 5}}
        events = [{"type": "message_start", "message": message}]
        index = 0
        if self.reasoning:
            events += [
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "thinking_delta", "thinking": self.reasoning}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "signature_delta", "signature": "stub"}},
                {"type": "content_block_stop", "index": 0},
            ]
            index = 1
        events += [
            {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": "391"}},
            {"type": "content_block_stop", "index": index},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


def _post(url: str, body: dict) -> bytes:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as resp:
        return resp.read()


def test_capture_proxy_passes_through_and_records(stub):
    with probe.CaptureProxy(stub.base_url) as capture:
        reply = _post(capture.url + "/v1/chat/completions", PI_BODY)
    assert b"391" in reply
    assert stub.requests == [("/v1/chat/completions", PI_BODY)]
    [exchange] = capture.model_requests()
    assert (exchange.api, exchange.status, exchange.request) == ("openai", 200, PI_BODY)
    assert exchange.response == reply


def test_probe_request_is_the_main_conversation_request():
    background = probe.Exchange("POST", "/v1/messages?beta=true", {"model": LLAMA, "messages": []})
    main = probe.Exchange("POST", "/v1/messages?beta=true", {"model": LLAMA, "tools": [{"name": "Bash"}]})
    hello = probe.Exchange("HEAD", "/api/hello", None)
    count = probe.Exchange("POST", "/v1/messages/count_tokens", {"model": LLAMA})
    assert probe.probe_request([hello, count, background, main], LLAMA) is main
    assert probe.probe_request([background], LLAMA) is background


def test_probe_request_refuses_another_model_or_no_request():
    with pytest.raises(probe.ProbeError, match="another model: other"):
        probe.probe_request([probe.Exchange("POST", "/v1/chat/completions", {"model": "other"})], LLAMA)
    with pytest.raises(probe.ProbeError, match="no model request"):
        probe.probe_request([probe.Exchange("HEAD", "/api/hello", None)], LLAMA)


def test_probe_with_a_stand_in_harness_records_the_result(tmp_path: Path, stub):
    def harness(entry, *, capture, run_dir, prompt, timeout):
        _post(capture.url + "/v1/messages?beta=true", dict(CC_BODY, model=entry, tools=[{"name": "Bash"}]))

    got = probe.probe("claude-code", LLAMA, base_url=stub.base_url, run_dir=tmp_path, runner=harness)
    assert (got.engine, got.api, got.observed) == ("llama.cpp", "anthropic", True)
    assert got.received.effort == "xhigh" and got.received.source == "template default"
    assert got.sent["output_config"] == {"effort": "high"}
    saved = json.loads((tmp_path / "probe.json").read_text())
    assert probe.ProbeResult.from_dict(saved) == got
    assert len((tmp_path / "requests.jsonl").read_text().splitlines()) == 1


def test_probe_refuses_an_entry_outside_the_arm_config(tmp_path: Path):
    with pytest.raises(probe.ProbeError, match="not an entry"):
        probe.probe("pi", "nope", base_url="http://127.0.0.1:9", run_dir=tmp_path)


# -- the real harnesses against the stub ---------------------------------------------------


@pytest.mark.skipif(not HAVE_PI, reason="pi is not installed")
@pytest.mark.timeout(300)
@pytest.mark.parametrize("entry", [LLAMA, STRATA])
def test_real_pi_sends_effort_xhigh_in_template_kwargs(tmp_path: Path, stub, entry):
    got = probe.probe("pi", entry, base_url=stub.base_url, run_dir=tmp_path, timeout=90)
    assert got.sent["chat_template_kwargs"]["enable_thinking"] is True
    assert got.sent["chat_template_kwargs"]["reasoning_effort"] == "xhigh"
    assert (got.received.thinking, got.received.effort, got.observed) == (True, "xhigh", True)
    assert {b.get("model") for p, b in stub.requests if p.endswith("/chat/completions")} == {entry}


@pytest.mark.skipif(not HAVE_CLAUDE, reason="the real claude binary is not installed")
@pytest.mark.timeout(300)
@pytest.mark.parametrize("entry", [LLAMA, STRATA])
def test_real_claude_code_sends_adaptive_thinking_and_an_effort(tmp_path: Path, stub, entry):
    got = probe.probe("claude-code", entry, base_url=stub.base_url, run_dir=tmp_path, timeout=120)
    assert got.sent["thinking"]["type"] == "adaptive"
    assert "effort" in got.sent["output_config"]
    assert got.received.thinking is True and got.observed is True
    if entry == LLAMA:
        assert got.received.source == "template default"
        assert got.received.effort == "xhigh"
    else:
        assert got.received.source == "output_config.effort"


# -- CLI -------------------------------------------------------------------------------


@pytest.fixture
def fake_rig(monkeypatch, tmp_path: Path):
    """The CLI with preflight, loading and the probe replaced; `efforts` sets each probe's effort."""
    calls: list[tuple] = []
    efforts: dict[tuple[str, str], str] = {}
    config = arms.load_config()

    def fake_preflight(run, base_url, state_path):
        calls.append(("preflight", run.entry))
        return []

    def fake_probe(harness, entry, *, base_url, run_dir, prompt, timeout, config):
        calls.append(("probe", harness, entry))
        return result(harness, entry, config.entries[entry]["engine"],
                      effort=efforts.get((harness, entry), "xhigh"))

    monkeypatch.setattr(preflight, "preflight", fake_preflight)
    monkeypatch.setattr(preflight, "record_bench_load", lambda path, entry: calls.append(("record", entry)))
    monkeypatch.setattr(probe, "load_entry", lambda base, entry, timeout: calls.append(("load", entry)))
    monkeypatch.setattr(probe, "probe", fake_probe)
    return calls, efforts, config


def test_cli_probes_each_entry_once_loaded_and_reports_same(fake_rig, tmp_path: Path, capsys):
    calls, _efforts, _config = fake_rig
    code = cmd_probe.main(["--harness", "pi", "--harness", "claude-code", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert calls == [
        ("preflight", LLAMA), ("record", LLAMA), ("load", LLAMA),
        ("probe", "pi", LLAMA), ("probe", "claude-code", LLAMA),
        ("preflight", STRATA), ("record", STRATA), ("load", STRATA),
        ("probe", "pi", STRATA), ("probe", "claude-code", STRATA),
    ]
    assert "pi: same on both engines (reasoning on, effort xhigh)" in out
    assert "claude-code: same on both engines (reasoning on, effort xhigh)" in out
    assert f"pi / {LLAMA} (llama.cpp, openai)" in out


def test_cli_reports_a_difference_and_exits_1(fake_rig, tmp_path: Path, capsys):
    _calls, efforts, _config = fake_rig
    efforts[("claude-code", STRATA)] = "medium"
    code = cmd_probe.main(["--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "pi: same on both engines" in out
    assert "claude-code: DIFFERENT - claude-code: refusing to compare" in out


def test_cli_json_output(fake_rig, tmp_path: Path, capsys):
    code = cmd_probe.main(["--harness", "pi", "--json", "--out", str(tmp_path)])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [p["entry"] for p in data["probes"]] == [LLAMA, STRATA]
    assert data["comparisons"] == [{"harness": "pi", "refusal": None}]


def test_cli_stops_on_a_preflight_refusal(monkeypatch, tmp_path: Path, capsys):
    def refuse(run, base_url, state_path):
        raise preflight.PreflightError("refusing to start: other-model", ["other-model"])

    monkeypatch.setattr(preflight, "preflight", refuse)
    assert cmd_probe.main(["--out", str(tmp_path)]) == 2
    assert "other-model" in capsys.readouterr().err


def test_cli_refuses_an_unknown_entry(tmp_path: Path, capsys):
    assert cmd_probe.main(["--entry", "nope", "--out", str(tmp_path)]) == 2
    assert "nope" in capsys.readouterr().err


def test_mb_dispatches_probe():
    import mb

    assert mb.main(["probe", "--entry", "nope"]) == 2


# -- rig -------------------------------------------------------------------------------


@pytest.mark.rig
@pytest.mark.timeout(3600)
def test_rig_probe_reports_the_same_setting_per_harness(tmp_path: Path, capsys):
    code = cmd_probe.main(["--harness", "pi", "--harness", "claude-code", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0, out
