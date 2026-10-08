"""The request shim: Strata's late-system rule, and a proxy that streams responses through."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from modelbench.msgshim import MessageShim, late_system_to_user


def test_late_system_messages_become_user_messages_in_place():
    body = {
        "system": [{"type": "text", "text": "prompt"}],
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": [{"type": "text", "text": "# Environment"}]},
            {"role": "assistant", "content": "ok"},
        ],
    }
    out = late_system_to_user(body)
    assert [m["role"] for m in out["messages"]] == ["user", "user", "assistant"]
    assert out["messages"][1]["content"] == body["messages"][1]["content"]
    assert out["system"] == body["system"]
    assert body["messages"][1]["role"] == "system"  # the input is not changed


def test_a_leading_system_message_stays_when_there_is_no_system_field():
    out = late_system_to_user({"messages": [{"role": "system", "content": "s"},
                                            {"role": "user", "content": "u"},
                                            {"role": "system", "content": "late"}]})
    assert [m["role"] for m in out["messages"]] == ["system", "user", "user"]


def test_a_body_without_messages_is_unchanged():
    assert late_system_to_user({"model": "m"}) == {"model": "m"}


class Upstream:
    """Records each request and answers POSTs with two server-sent events."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes]] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_HEAD(self):
                upstream.requests.append(("HEAD", self.path, b""))
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("content-length") or 0))
                upstream.requests.append(("POST", self.path, data))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for n in range(2):
                    self.wfile.write(f"event: e\ndata: {n}\n\n".encode())
                    self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def proxied():
    upstream = Upstream()
    shim = MessageShim(upstream.url)
    yield upstream, shim
    shim.close()
    upstream.close()


def _post(url: str, body: dict) -> tuple[int, str, str]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.headers["content-type"], resp.read().decode()


@pytest.mark.parametrize("path", ["/v1/messages?beta=true", "/v1/messages/count_tokens?beta=true"])
def test_messages_requests_are_rewritten_and_the_stream_comes_back(proxied, path):
    upstream, shim = proxied
    body = {"system": "s", "messages": [{"role": "user", "content": "u"},
                                        {"role": "system", "content": "late"}]}
    status, ctype, text = _post(shim.url + path, body)
    assert (status, ctype) == (200, "text/event-stream")
    assert text == "event: e\ndata: 0\n\nevent: e\ndata: 1\n\n"
    method, seen_path, data = upstream.requests[-1]
    assert (method, seen_path) == ("POST", path)
    assert [m["role"] for m in json.loads(data)["messages"]] == ["user", "user"]


def test_other_requests_pass_through_unchanged(proxied):
    upstream, shim = proxied
    body = {"messages": [{"role": "user", "content": "u"}, {"role": "system", "content": "late"}]}
    _post(shim.url + "/v1/chat/completions", body)
    assert json.loads(upstream.requests[-1][2]) == body
    req = urllib.request.Request(shim.url + "/api/hello", method="HEAD")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
    assert upstream.requests[-1][:2] == ("HEAD", "/api/hello")


def test_an_unreachable_upstream_is_a_502():
    upstream = Upstream()
    upstream.close()
    shim = MessageShim(upstream.url)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(shim.url + "/v1/messages", {"messages": []})
        assert err.value.code == 502
    finally:
        shim.close()
