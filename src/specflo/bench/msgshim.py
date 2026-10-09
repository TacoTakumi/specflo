"""A local proxy between Claude Code and llama-swap that moves late system messages.

Claude Code sends {"role": "system"} messages inside `messages` (the
environment block after the first user turn) to any model id it does not know.
llama-server's /v1/messages converter passes them through, and the Qwen chat
template raises "System message must be at the beginning": an HTTP 500, which
Claude Code does not treat as a refusal of those messages (its fallback needs a
400 or 422), so every request fails.

Strata turns such messages into user messages in place
(`serve/frontend.py _late_system_to_user`). This proxy applies the same rule to
/v1/messages requests before they reach a llama.cpp entry, so both engines see
the same prompt shape. Everything else passes through unchanged, and responses
(server-sent events included) are streamed back as they arrive.
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

NAME = "late-system-to-user"

# Not forwarded: hop-by-hop headers, and the ones the proxy sets itself.
_SKIP_REQUEST = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
_SKIP_RESPONSE = {"connection", "transfer-encoding", "keep-alive"}


def late_system_to_user(body: dict[str, Any]) -> dict[str, Any]:
    """Strata's rule on an Anthropic body: a system message that is not the first becomes a user message.

    The top-level `system` field is the first system message, so with it every
    system message in `messages` is late; without it, only messages[0] is not.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body
    first = 0 if body.get("system") else 1
    return dict(body, messages=[
        dict(m, role="user") if isinstance(m, dict) and m.get("role") == "system" and i >= first else m
        for i, m in enumerate(messages)
    ])


class MessageShim:
    """The proxy, serving on 127.0.0.1 on a free port from construction until `close()`."""

    def __init__(self, upstream: str) -> None:
        parts = urlsplit(upstream)
        self.upstream = upstream
        target = (parts.hostname or "localhost", parts.port or 80)
        prefix = parts.path.rstrip("/")

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
                if data and self.command == "POST" and self.path.startswith("/v1/messages"):
                    try:
                        data = json.dumps(late_system_to_user(json.loads(data))).encode()
                    except ValueError:
                        pass  # not JSON: the server answers it
                headers = {k: v for k, v in self.headers.items() if k.lower() not in _SKIP_REQUEST}
                conn = http.client.HTTPConnection(*target, timeout=3600)
                sent = False
                try:
                    conn.request(self.command, prefix + self.path, body=data, headers=headers)
                    resp = conn.getresponse()
                    self.send_response(resp.status, resp.reason)
                    for key, value in resp.getheaders():
                        if key.lower() not in _SKIP_RESPONSE:
                            self.send_header(key, value)
                    self.end_headers()
                    sent = True
                    while chunk := resp.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except OSError as exc:
                    if not sent:
                        self.send_error(502, f"upstream {upstream}: {exc}")
                finally:
                    conn.close()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
