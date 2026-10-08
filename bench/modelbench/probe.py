"""Reasoning-setting probe: what reasoning setting each engine gets through each harness.

Within one harness both engines must run at the same reasoning setting. The
probe shows the setting before an arm's first run: it runs the real harness
once with a one-line prompt, through a capturing proxy in front of llama-swap,
so it sees the exact request body the harness sent and the response that came
back. Each harness keeps its own API path: pi talks OpenAI chat completions
(`/v1/chat/completions`), Claude Code talks Anthropic messages (`/v1/messages`).

What the engine received is derived from the captured body with the engine's
own request rules (`llama_cpp_receives`, `strata_receives`), which mirror the
engine sources:

- llama.cpp, `tools/server/server-chat.cpp server_chat_convert_anthropic_to_oai`:
  a /v1/messages body keeps only temperature, top_p, top_k, stream and
  chat_template_kwargs, and `thinking.type == "enabled"` becomes
  `thinking_budget_tokens`. `output_config.effort` and any other `thinking`
  type (Claude Code's `adaptive`, also `disabled`) are dropped. Then
  `server-common.cpp` (both APIs): thinking is on by default,
  `chat_template_kwargs.enable_thinking` true or false sets it,
  `chat_template_kwargs.reasoning_effort` goes to the template, and a top-level
  `reasoning_effort` overrides it ("none" turns thinking off).
- Strata, `serve/frontend.py`: the OpenAI path reads `reasoning_effort` (or
  `reasoning.effort`), then `chat_template_kwargs` (enable_thinking false wins,
  reasoning_effort next); the Anthropic path reads `thinking.type ==
  "disabled"`, then `output_config.effort`, then `thinking.budget_tokens`. Its
  EFFORT table maps client spellings onto the template's levels ("high" is
  "xhigh"). `serve/server.py Service.with_shared` first fills in the web app's
  shared Chat settings where the request names no level; the probe reads them
  from the entry's GET /settings.

The chat template of both entries defaults to effort "xhigh" when thinking is
on and no effort is given (`reasoning_effort|default('xhigh')`). "high" is
"xhigh" on both: llama.cpp's template maps it, Strata's table maps it before
its template. So the effort compared is the level the template renders, not
the field that carried it.

Whether reasoning was on is also read from the response: any non-empty
`reasoning_content` (OpenAI) or thinking block (Anthropic). A probe whose
response disagrees with its derivation, or has no readable response, does not
vouch for the setting.

`require_same` is what the batch calls with the two probes of one harness: it
raises ProbeRefusal when they differ, so that comparison does not start.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from modelbench import arms, launch_cc, launch_pi

ENGINES = ("llama.cpp", "strata")
API_OF_HARNESS = {"pi": "openai", "claude-code": "anthropic"}

PROMPT = "What is 17 times 23? Reply with the number only."
DEFAULT_TIMEOUT = 900.0

# The chat template's level when thinking is on and no effort is given, and
# its reading of "high".
TEMPLATE_DEFAULT_EFFORT = "xhigh"
_TEMPLATE_ALIASES = {"high": "xhigh"}

# Strata's EFFORT table (serve/frontend.py): client spellings -> template level; None is off.
STRATA_EFFORT = {
    "none": None, "off": None, "minimal": None, "disabled": None, "false": None,
    "low": "low", "medium": "medium", "high": "xhigh", "xhigh": "xhigh",
    "max": "xhigh", "maximum": "xhigh",
}

# Request fields that can carry a reasoning setting, per API.
REASONING_FIELDS = {
    "anthropic": ("thinking", "output_config", "chat_template_kwargs", "reasoning_budget_tokens"),
    "openai": ("reasoning_effort", "reasoning", "chat_template_kwargs",
               "thinking_budget_tokens", "reasoning_budget_tokens"),
}

# Hermetic harness runs: specflo's pi extension shells out to a no-op and keeps
# its control server off; Claude Code's SessionStart hook finds a no-op
# `specflo` first on PATH.
_PI_HERMETIC = {"SPECFLO_BIN": "true", "SPECFLO_AGENT_SERVE": "0"}

_SKIP_REQUEST = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
_SKIP_RESPONSE = {"connection", "transfer-encoding", "keep-alive"}


class ProbeError(RuntimeError):
    """The probe could not be run or read."""


class ProbeRefusal(RuntimeError):
    """The two engines of one harness got different reasoning settings: do not compare them."""

    def __init__(self, message: str, differences: list[str]) -> None:
        self.differences = differences
        super().__init__(message)


# -- what the engine receives ------------------------------------------------------


@dataclass(frozen=True)
class Received:
    """The reasoning setting an engine renders the prompt with.

    `effort` is the template's level (None when thinking is off); `source`
    names the request field that decided it; `ignored` lists reasoning fields
    the request carried that the engine drops.
    """

    thinking: bool
    effort: str | None
    source: str
    budget: int | None = None
    ignored: tuple[str, ...] = ()


def template_effort(effort: str | None) -> str:
    """The level the chat template renders for a given reasoning_effort kwarg."""
    if effort is None or effort == "":
        return TEMPLATE_DEFAULT_EFFORT
    return _TEMPLATE_ALIASES.get(effort, effort)


def _api_of(path: str) -> str | None:
    path = path.split("?")[0]
    if path.endswith("/chat/completions"):
        return "openai"
    if path.rstrip("/").endswith("/v1/messages"):
        return "anthropic"
    return None


def llama_cpp_receives(body: Mapping[str, Any], api: str) -> Received:
    """What llama-server's template gets from `body` on `api` ("openai" or "anthropic")."""
    ignored: list[str] = []
    if api == "anthropic":
        converted: dict[str, Any] = {}
        if "chat_template_kwargs" in body:
            converted["chat_template_kwargs"] = body["chat_template_kwargs"]
        thinking = body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            converted["thinking_budget_tokens"] = thinking.get("budget_tokens", 10000)
        elif thinking is not None:
            kind = thinking.get("type") if isinstance(thinking, dict) else thinking
            ignored.append(f"thinking.type={kind}")
        config = body.get("output_config")
        if isinstance(config, dict) and "effort" in config:
            ignored.append(f"output_config.effort={config['effort']}")
        if "reasoning_budget_tokens" in body:
            ignored.append("reasoning_budget_tokens")
        body = converted
    elif api != "openai":
        raise ValueError(f"unknown api {api!r}")

    thinking_on, effort, source = True, None, "template default"
    kwargs = body.get("chat_template_kwargs")
    kwargs = kwargs if isinstance(kwargs, dict) else {}
    if kwargs.get("enable_thinking") is True:
        thinking_on, source = True, "chat_template_kwargs.enable_thinking"
    elif kwargs.get("enable_thinking") is False:
        thinking_on, source = False, "chat_template_kwargs.enable_thinking"
    if kwargs.get("reasoning_effort") is not None:
        effort, source = str(kwargs["reasoning_effort"]), "chat_template_kwargs.reasoning_effort"
    top = body.get("reasoning_effort")
    if top == "none":
        thinking_on, effort, source = False, None, "reasoning_effort"
    elif isinstance(top, str) and top:
        effort, source = top, "reasoning_effort"
    budget = body.get("reasoning_budget_tokens", body.get("thinking_budget_tokens"))
    return Received(
        thinking=thinking_on,
        effort=template_effort(effort) if thinking_on else None,
        source=source,
        budget=budget if isinstance(budget, int) and budget >= 0 else None,
        ignored=tuple(ignored),
    )


def _strata_effort(value: Any) -> dict[str, Any]:
    """Strata's effort_kwargs: a client effort -> template kwargs."""
    if value is None or value == "":
        return {}
    if value is False:
        return {"enable_thinking": False}
    key = str(value).strip().lower()
    if key not in STRATA_EFFORT:
        raise ProbeError(f"Strata refuses reasoning effort {value!r} (HTTP 400)")
    level = STRATA_EFFORT[key]
    return {"enable_thinking": False} if level is None else {"reasoning_effort": level}


def _strata_budget_effort(tokens: Any) -> dict[str, Any]:
    try:
        n = int(tokens)
    except (TypeError, ValueError):
        return {}
    return {"reasoning_effort": "low" if n < 2048 else "medium" if n < 8192 else "xhigh"}


def _strata_with_shared(body: Mapping[str, Any], api: str, shared: Mapping[str, Any]) -> dict[str, Any]:
    """Strata's Service.with_shared: the shared Chat settings' level where the request names none."""
    req = dict(body)
    effort = shared.get("reasoning_effort")
    if not effort:
        return req
    if api == "openai":
        kwargs = req.get("chat_template_kwargs") if isinstance(req.get("chat_template_kwargs"), dict) else {}
        if not req.get("reasoning_effort") and not req.get("reasoning") and \
                "enable_thinking" not in kwargs and "reasoning_effort" not in kwargs:
            req["reasoning_effort"] = effort
    elif not req.get("thinking") and not req.get("output_config"):
        if effort == "none":
            req["thinking"] = {"type": "disabled"}
        else:
            req["output_config"] = {"effort": effort}
    return req


def strata_receives(
    body: Mapping[str, Any],
    api: str,
    *,
    shared: Mapping[str, Any] | None = None,
    think_unasked: bool = True,
) -> Received:
    """What Strata's template gets from `body` on `api`.

    `shared` is the entry's shared Chat settings (GET /settings `defaults`);
    `think_unasked` is False only under the config's "anthropic_thinking":
    "on_request".
    """
    req = _strata_with_shared(body, api, shared or {})
    source = "template default"
    if api == "openai":
        reasoning = req.get("reasoning") if isinstance(req.get("reasoning"), dict) else {}
        top = req.get("reasoning_effort") or reasoning.get("effort")
        kwargs = _strata_effort(top)
        if kwargs:
            source = "reasoning_effort" if req.get("reasoning_effort") else "reasoning.effort"
        template_kwargs = req.get("chat_template_kwargs")
        for key, value in (template_kwargs if isinstance(template_kwargs, dict) else {}).items():
            if key == "enable_thinking" and not value:
                kwargs, source = {"enable_thinking": False}, "chat_template_kwargs.enable_thinking"
            elif key == "reasoning_effort" and "enable_thinking" not in kwargs:
                kwargs.update(_strata_effort(value))
                source = "chat_template_kwargs.reasoning_effort"
    elif api == "anthropic":
        kwargs = {}
        thinking = req.get("thinking")
        config = req.get("output_config")
        effort = config.get("effort") if isinstance(config, dict) else None
        if isinstance(thinking, dict) and thinking.get("type") == "disabled":
            kwargs, source = {"enable_thinking": False}, "thinking.type=disabled"
        elif effort:
            kwargs, source = _strata_effort(effort), "output_config.effort"
        elif isinstance(thinking, dict) and thinking.get("budget_tokens"):
            kwargs, source = _strata_budget_effort(thinking["budget_tokens"]), "thinking.budget_tokens"
        elif thinking is None and not req.get("reasoning_budget_tokens") and not think_unasked:
            kwargs, source = {"enable_thinking": False}, "anthropic_thinking on_request"
    else:
        raise ValueError(f"unknown api {api!r}")
    if req != dict(body):
        source += " (filled in from the shared Chat settings)"
    thinking_on = kwargs.get("enable_thinking", True) is not False
    budget = req.get("reasoning_budget_tokens")
    return Received(
        thinking=thinking_on,
        effort=template_effort(kwargs.get("reasoning_effort")) if thinking_on else None,
        source=source,
        budget=budget if isinstance(budget, int) and budget > 0 else None,
    )


def engine_receives(
    engine: str, body: Mapping[str, Any], api: str, *, shared: Mapping[str, Any] | None = None
) -> Received:
    """Dispatch to the engine's rules."""
    if engine == "llama.cpp":
        return llama_cpp_receives(body, api)
    if engine == "strata":
        return strata_receives(body, api, shared=shared)
    raise ValueError(f"unknown engine {engine!r} (known: {', '.join(ENGINES)})")


def sent_fields(body: Mapping[str, Any], api: str) -> dict[str, Any]:
    """The reasoning fields of a request body, as sent."""
    return {name: body[name] for name in REASONING_FIELDS[api] if name in body}


# -- what the response shows --------------------------------------------------------


def _events(raw: bytes) -> list[dict[str, Any]]:
    """The JSON payloads of a server-sent-event stream, or the one JSON body."""
    text = raw.decode("utf-8", errors="replace")
    events = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    if events:
        return events
    try:
        obj = json.loads(text)
    except ValueError:
        return []
    return [obj] if isinstance(obj, dict) else []


def response_reasoning(api: str, raw: bytes) -> int | None:
    """Characters of reasoning in a response (0: none), or None when it holds no reply."""
    events = _events(raw)
    replies = 0
    chars = 0
    for event in events:
        if "error" in event or event.get("type") == "error":
            return None
        if api == "openai":
            for choice in event.get("choices") or []:
                replies += 1
                part = choice.get("delta") or choice.get("message") or {}
                chars += len(str(part.get("reasoning_content") or "").strip())
        else:
            kind = event.get("type")
            if kind in ("message_start", "message"):
                replies += 1
            if kind == "message":
                blocks = event.get("content") or []
            elif kind == "content_block_start":
                blocks = [event.get("content_block") or {}]
            elif kind == "content_block_delta" and (event.get("delta") or {}).get("type") == "thinking_delta":
                blocks = [{"type": "thinking", "thinking": event["delta"].get("thinking")}]
            else:
                blocks = []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    chars += len(str(block.get("thinking") or "").strip())
    return chars if replies else None


# -- capture ---------------------------------------------------------------------------


@dataclass
class Exchange:
    """One request through the capture proxy and what came back."""

    method: str
    path: str
    request: Any
    status: int | None = None
    response: bytes = field(default=b"", repr=False)

    @property
    def api(self) -> str | None:
        return _api_of(self.path) if self.method == "POST" else None


class CaptureProxy:
    """A recording reverse proxy on 127.0.0.1, from construction until `close()`.

    Requests and responses pass through unchanged (server-sent events stream
    back as they arrive); each is recorded as an Exchange.
    """

    def __init__(self, upstream: str) -> None:
        parts = urlsplit(upstream)
        self.upstream = upstream
        self.exchanges: list[Exchange] = []
        lock = threading.Lock()
        target = (parts.hostname or "localhost", parts.port or 80)
        prefix = parts.path.rstrip("/")
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def do_GET(self) -> None:
                self._forward()

            def do_HEAD(self) -> None:
                self._forward()

            def do_POST(self) -> None:
                self._forward()

            def _forward(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                data = self.rfile.read(length) if length else None
                try:
                    parsed = json.loads(data) if data else None
                except ValueError:
                    parsed = None
                exchange = Exchange(self.command, self.path, parsed)
                with lock:
                    proxy.exchanges.append(exchange)
                headers = {k: v for k, v in self.headers.items() if k.lower() not in _SKIP_REQUEST}
                conn = http.client.HTTPConnection(*target, timeout=3600)
                sent = False
                received = bytearray()
                try:
                    conn.request(self.command, prefix + self.path, body=data, headers=headers)
                    resp = conn.getresponse()
                    exchange.status = resp.status
                    self.send_response(resp.status, resp.reason)
                    for key, value in resp.getheaders():
                        if key.lower() not in _SKIP_RESPONSE:
                            self.send_header(key, value)
                    self.end_headers()
                    sent = True
                    while chunk := resp.read1(65536):
                        received += chunk
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except OSError as exc:
                    if not sent:
                        self.send_error(502, f"upstream {upstream}: {exc}")
                finally:
                    exchange.response = bytes(received)
                    conn.close()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def model_requests(self) -> list[Exchange]:
        """The captured inference requests (chat completions or messages), in order."""
        return [e for e in self.exchanges if e.api and isinstance(e.request, dict)]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> CaptureProxy:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def probe_request(exchanges: Sequence[Exchange], entry: str) -> Exchange:
    """The request the probe reads: the first that offers tools (the main
    conversation, not a background call), else the first.

    Raises ProbeError when no request was captured or one named another model.
    """
    requests = [e for e in exchanges if e.api and isinstance(e.request, dict)]
    if not requests:
        raise ProbeError("the harness sent no model request")
    others = sorted({str(e.request.get("model")) for e in requests} - {entry})
    if others:
        raise ProbeError(f"the harness named another model: {', '.join(others)}")
    return next((e for e in requests if e.request.get("tools")), requests[0])


# -- probe result and comparison -----------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """One harness, one entry: what was sent, what the engine got, what the reply shows."""

    harness: str
    entry: str
    engine: str
    api: str
    sent: dict[str, Any]
    received: Received
    reasoning_chars: int | None
    requests: int = 1
    engine_settings: dict[str, Any] | None = None

    @property
    def observed(self) -> bool | None:
        """Reasoning on in the reply, or None when there was no readable reply."""
        return None if self.reasoning_chars is None else self.reasoning_chars > 0

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["received"]["ignored"] = list(self.received.ignored)
        out["observed"] = self.observed
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProbeResult:
        received = dict(data["received"])
        received["ignored"] = tuple(received.get("ignored") or ())
        return cls(
            harness=data["harness"], entry=data["entry"], engine=data["engine"], api=data["api"],
            sent=dict(data.get("sent") or {}), received=Received(**received),
            reasoning_chars=data.get("reasoning_chars"), requests=data.get("requests", 1),
            engine_settings=data.get("engine_settings"),
        )


def _on(value: bool | None) -> str:
    return "unknown" if value is None else "on" if value else "off"


def self_check(result: ProbeResult) -> list[str]:
    """Problems with one probe: no readable reply, or a reply that contradicts the derivation."""
    name = f"{result.entry} ({result.engine})"
    if result.observed is None:
        return [f"{name}: no readable reply to check reasoning against"]
    if result.observed != result.received.thinking:
        return [f"{name}: the reply shows reasoning {_on(result.observed)}, "
                f"the request rules give {_on(result.received.thinking)}"]
    return []


def differences(a: ProbeResult, b: ProbeResult) -> list[str]:
    """Why two probes of one harness must not be compared; empty when they match."""
    if a.harness != b.harness:
        raise ValueError(f"probes of different harnesses: {a.harness} and {b.harness}")
    out = self_check(a) + self_check(b)
    ra, rb = a.received, b.received
    for label, va, vb in (("reasoning", _on(ra.thinking), _on(rb.thinking)),
                          ("effort", ra.effort or "none", rb.effort or "none"),
                          ("thinking budget", ra.budget, rb.budget)):
        if va != vb:
            out.append(f"{label}: {a.entry} ({a.engine}) {va}, {b.entry} ({b.engine}) {vb}")
    return out


def refusal(a: ProbeResult, b: ProbeResult) -> str | None:
    """The refusal message for comparing `a` with `b`, or None when they may be compared."""
    found = differences(a, b)
    if not found:
        return None
    return (f"{a.harness}: refusing to compare {a.entry} with {b.entry}: the engines got "
            f"different reasoning settings ({'; '.join(found)})")


def require_same(a: ProbeResult, b: ProbeResult) -> None:
    """What the batch calls before a comparison: raise ProbeRefusal when the two probes differ."""
    message = refusal(a, b)
    if message:
        raise ProbeRefusal(message, differences(a, b))


# -- rig steps -----------------------------------------------------------------------


def _get(url: str, timeout: float) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise ProbeError(f"GET {url}: {exc}") from None


def load_entry(base_url: str, entry: str, timeout: float = DEFAULT_TIMEOUT) -> None:
    """Have llama-swap load `entry` with no inference: GET its upstream /health."""
    url = f"{base_url.rstrip('/')}/upstream/{entry}/health"
    status, body = _get(url, timeout)
    if status != 200:
        raise ProbeError(f"loading {entry}: GET {url} answered {status}: {body[:200]!r}")


def strata_shared_settings(base_url: str, entry: str, timeout: float = 30.0) -> dict[str, Any] | None:
    """The Strata entry's shared Chat settings (GET /settings `defaults`), or None if unreadable."""
    try:
        status, body = _get(f"{base_url.rstrip('/')}/upstream/{entry}/settings", timeout)
        data = json.loads(body) if status == 200 else None
    except (ProbeError, ValueError):
        return None
    defaults = data.get("defaults") if isinstance(data, dict) else None
    return defaults if isinstance(defaults, dict) else None


def _wait(proc: subprocess.Popen, timeout: float, name: str, err_path: Path) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise ProbeError(f"{name} did not exit in {timeout:.0f} s") from None
    if proc.returncode != 0:
        tail = err_path.read_text(encoding="utf-8", errors="replace")[-1500:]
        raise ProbeError(f"{name} exited {proc.returncode}: {tail}")


def _start_logged(start: Any, launch: Any, run_dir: Path, name: str) -> tuple[subprocess.Popen, Path]:
    out, err = run_dir / f"{name}.out", run_dir / f"{name}.err"
    with out.open("wb") as fo, err.open("wb") as fe:
        proc = start(launch, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe)
    return proc, err


def run_pi(
    entry: str, *, capture: CaptureProxy, run_dir: Path, prompt: str = PROMPT,
    timeout: float = DEFAULT_TIMEOUT, pi_bin: str = "pi",
) -> None:
    """Run pi once on `prompt`, its provider pointed at the capture proxy.

    The run copy's models.json gets the proxy's address after the launcher
    hashed it; the frozen directory is never touched.
    """
    workdir = run_dir / "work"
    workdir.mkdir(parents=True)
    launch = launch_pi.build_launch(
        entry, workdir=workdir, run_dir=run_dir, pi_bin=pi_bin,
        args=["--mode", "json", "--no-session", prompt], extra_env=_PI_HERMETIC,
    )
    frozen = launch_pi.load_frozen()
    models_path = launch.agent_dir / launch_pi.MODELS_FILE
    models = json.loads(models_path.read_text(encoding="utf-8"))
    provider = models["providers"][frozen.provider]
    provider["baseUrl"] = capture.url + urlsplit(provider["baseUrl"]).path
    models_path.write_text(json.dumps(models, indent=2) + "\n", encoding="utf-8")
    proc, err = _start_logged(launch_pi.start, launch, run_dir, "pi")
    _wait(proc, timeout, "pi", err)


def run_claude_code(
    entry: str, *, capture: CaptureProxy, run_dir: Path, prompt: str = PROMPT,
    timeout: float = DEFAULT_TIMEOUT, claude_bin: str | None = None,
) -> None:
    """Run Claude Code once on `prompt` with the capture proxy as its base URL.

    For a llama.cpp entry the launcher's own message shim sits between Claude
    Code and the proxy, so the proxy sees what the engine gets.
    """
    workdir = run_dir / "work"
    workdir.mkdir(parents=True)
    bindir = run_dir / "bin"
    bindir.mkdir()
    noop = bindir / "specflo"
    noop.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    noop.chmod(0o755)
    path = f"{bindir}:{os.environ.get('PATH', '')}"
    with launch_cc.build_launch(
        entry, workdir=workdir, run_dir=run_dir, claude_bin=claude_bin, base_url=capture.url,
        extra_env={"PATH": path},
        args=["-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence", prompt],
    ) as launch:
        proc, err = _start_logged(launch_cc.start, launch, run_dir, "claude")
        _wait(proc, timeout, "claude", err)


RUNNERS = {"pi": run_pi, "claude-code": run_claude_code}


def save_exchanges(exchanges: Sequence[Exchange], path: Path) -> None:
    """Write the captured requests (bodies and raw responses) as JSON lines."""
    with path.open("w", encoding="utf-8") as fh:
        for e in exchanges:
            fh.write(json.dumps({
                "method": e.method, "path": e.path, "status": e.status, "request": e.request,
                "response": e.response.decode("utf-8", errors="replace"),
            }) + "\n")


def result_from_capture(
    harness: str, entry: str, engine: str, exchanges: Sequence[Exchange],
    *, engine_settings: Mapping[str, Any] | None = None,
) -> ProbeResult:
    """Build the probe result from what the capture proxy recorded."""
    chosen = probe_request(exchanges, entry)
    api = chosen.api or API_OF_HARNESS[harness]
    body = chosen.request
    return ProbeResult(
        harness=harness, entry=entry, engine=engine, api=api,
        sent=sent_fields(body, api),
        received=engine_receives(engine, body, api, shared=engine_settings),
        reasoning_chars=response_reasoning(api, chosen.response) if chosen.status == 200 else None,
        requests=len([e for e in exchanges if e.api and isinstance(e.request, dict)]),
        engine_settings=dict(engine_settings) if engine_settings is not None else None,
    )


def probe(
    harness: str,
    entry: str,
    *,
    base_url: str,
    run_dir: Path | str | None = None,
    prompt: str = PROMPT,
    timeout: float = DEFAULT_TIMEOUT,
    config: arms.Config | None = None,
    runner: Any = None,
) -> ProbeResult:
    """Run `harness` once against `entry` through the capture proxy and read the result.

    `base_url` is llama-swap's; the entry must be loaded or loadable (the
    caller does preflight). The captured requests are saved under `run_dir`.
    """
    config = config or arms.load_config()
    if entry not in config.entries:
        raise ProbeError(f"entry {entry!r} is not an entry of the arm config")
    if harness not in RUNNERS:
        raise ProbeError(f"unknown harness {harness!r} (known: {', '.join(RUNNERS)})")
    engine = config.entries[entry]["engine"]
    run_dir = Path(run_dir) if run_dir else Path(tempfile.mkdtemp(prefix="mb-probe-"))
    run_dir.mkdir(parents=True, exist_ok=True)
    run = runner or RUNNERS[harness]
    with CaptureProxy(base_url) as capture:
        try:
            run(entry, capture=capture, run_dir=run_dir, prompt=prompt, timeout=timeout)
        finally:
            save_exchanges(capture.exchanges, run_dir / "requests.jsonl")
        exchanges = list(capture.exchanges)
    settings = strata_shared_settings(base_url, entry) if engine == "strata" else None
    result = result_from_capture(harness, entry, engine, exchanges, engine_settings=settings)
    (run_dir / "probe.json").write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    return result
