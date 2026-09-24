"""A daemon's 200 answer that the client cannot use gives the verb's error.

An answer that is not JSON, one with no result, one whose result has the
wrong type, and one whose record lacks a field it needs: each makes the verb
that asked exit non-zero with one error line naming what the daemon answered,
and never a traceback. The daemon here is a loopback server that answers every
request with the same 200 body.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from specflo import config
from specflo.cli import app

runner = CliRunner()

ANSWERS = {
    "not-json": "<html>a proxy's error page</html>",
    "no-result": '{"answer": 1}',
    "wrong-type": '{"result": 7}',
    "missing-field": '{"result": {}}',
}

VERBS = {
    "project": ["task", "start", "T-01"],
    "product": ["product", "show", "my-thing"],
    "work-item": ["workitem", "show", "1"],
}


@pytest.fixture
def answering():
    """A loopback server that answers 200 with the body set on it."""
    body: dict[str, str] = {"text": ""}

    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            data = body["text"].encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = _answer

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield body, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def checkout(tmp_path, monkeypatch, answering):
    """A checkout whose one remote is the answering server, holding its active project."""
    _, url = answering
    root = tmp_path / "checkout"
    root.mkdir()
    cfg = config.init_config(root)
    config.add_remote(root, "home", url, "s3cret")
    config.record_hosted_project(root, "demo", "home")
    cfg.active_project = "demo"
    config.save_config(root, cfg)
    monkeypatch.chdir(root)
    return root


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("answer", ANSWERS)
def test_a_malformed_answer_gives_one_error_line_and_no_traceback(
    checkout, answering, verb, answer
):
    body, url = answering
    body["text"] = ANSWERS[answer]

    result = runner.invoke(app, VERBS[verb])

    assert result.exit_code not in (0, None)
    assert not isinstance(result.exception, (ValueError, KeyError, TypeError)), result.exception
    assert "Traceback" not in result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1, result.output
    assert url in lines[0] and "malformed" in lines[0]
    assert ANSWERS[answer][:20] in lines[0]
