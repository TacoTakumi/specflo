"""`specflo bench doctor`: the bench setup check, one line per check."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from specflo import settings
from specflo.bench import DATA
from specflo.bench import doctor as bench_doctor
from specflo.cli import app

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A daemon root R in the environment; the bench home is R/bench."""
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv(settings.DAEMON_ROOT_ENV, str(tmp_path / "R"))
    bench = tmp_path / "R" / "bench"
    bench.mkdir(parents=True)
    return bench


class StubSwap:
    """A llama-swap that lists `models` at /v1/models and records every request."""

    def __init__(self, models):
        self.models = models
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                stub.requests.append((self.command, self.path))
                if self.path != "/v1/models":
                    self.send_error(404)
                    return
                body = json.dumps({"data": [{"id": m} for m in stub.models]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                stub.requests.append((self.command, self.path))
                self.send_error(500)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _closed_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _arms(home, *entries):
    lines = ["entries:"] + [f"  {e}: {{engine: llama.cpp}}" for e in entries]
    lines += ["harnesses: [pi]", "levels: {quick: {}}"]
    (home / "arms.yaml").write_text("\n".join(lines) + "\n")


def _by_subject(checks, subject):
    return [c for c in checks if c.subject == subject]


def test_report_fails_with_a_fix_and_exit_1_when_a_check_fails(home):
    result = runner.invoke(app, ["bench", "doctor"])

    assert result.exit_code == 1
    assert any(line.startswith("FAIL") and "Fix:" in line for line in result.stdout.splitlines())


def test_report_json_carries_ok_and_every_check(home):
    result = runner.invoke(app, ["bench", "doctor", "--json"])

    data = json.loads(result.stdout)
    assert result.exit_code == 1
    assert data["ok"] is False
    assert {"status", "subject", "detail", "fix"} <= set(data["checks"][0])


def test_report_exits_0_when_no_check_fails(home, monkeypatch):
    monkeypatch.setattr(
        bench_doctor, "run_checks", lambda root, **kw: [bench_doctor.Check("ok", "x", "fine")]
    )

    result = runner.invoke(app, ["bench", "doctor"])

    assert result.exit_code == 0, result.output
    assert "All checks passed." in result.stdout


def test_root_unset_fails_naming_serve_root_without_a_prompt(home, monkeypatch):
    monkeypatch.delenv(settings.DAEMON_ROOT_ENV)
    monkeypatch.setattr("specflo.daemon.cli._stdin_is_terminal", lambda: True)

    result = runner.invoke(app, ["bench", "doctor"], input="")

    assert result.exit_code == 1
    assert "Daemon root" not in result.output
    [root] = [line for line in result.stdout.splitlines() if "  daemon root:" in line]
    assert root.startswith("FAIL") and "specflo serve root" in root


def test_root_set_is_ok_and_names_its_source(home):
    [check] = _by_subject(bench_doctor.run_checks(None), "daemon root")

    assert check.status == "ok"
    assert str(home.parent) in check.detail
    assert settings.DAEMON_ROOT_ENV in check.detail


def test_config_valid_file_is_ok(home):
    (home / "arms.yaml").write_text((DATA / "arms.yaml").read_text())

    [check] = _by_subject(bench_doctor.run_checks(None), "bench config")

    assert check.status == "ok"


def test_config_missing_fails_naming_bench_init(home):
    [check] = _by_subject(bench_doctor.run_checks(None), "bench config")

    assert check.status == "fail"
    assert "specflo bench init" in check.fix


def test_config_without_entries_fails_naming_the_field(home):
    (home / "arms.yaml").write_text("harnesses: [pi]\nlevels: {quick: {}}\n")

    [check] = _by_subject(bench_doctor.run_checks(None), "bench config")

    assert check.status == "fail"
    assert "entries" in check.detail


def test_llama_swap_down_fails_naming_the_url(home):
    _arms(home, "a")
    url = _closed_url()

    [check] = _by_subject(bench_doctor.run_checks(None, base_url=url), "llama-swap")

    assert check.status == "fail"
    assert f"{url}/v1/models" in check.detail


def test_llama_swap_one_entry_absent_gives_one_fail_naming_it(home):
    _arms(home, "a", "b")
    with StubSwap(["a", "other"]) as stub:
        checks = bench_doctor.run_checks(None, base_url=stub.url)

    fails = [c for c in checks if c.subject.startswith("llama-swap") and c.status == "fail"]
    assert len(fails) == 1
    assert "b" in fails[0].subject
    assert stub.requests == [("GET", "/v1/models")]


def test_llama_swap_every_entry_listed_is_ok(home):
    _arms(home, "a", "b")
    with StubSwap(["b", "a"]) as stub:
        checks = bench_doctor.run_checks(None, base_url=stub.url)

    swap = [c for c in checks if c.subject.startswith("llama-swap")]
    assert [c.status for c in swap] == ["ok"]
    assert stub.requests == [("GET", "/v1/models")]


def test_llama_swap_without_a_config_checks_only_that_it_answers(home):
    with StubSwap([]) as stub:
        checks = bench_doctor.run_checks(None, base_url=stub.url)

    [check] = [c for c in checks if c.subject.startswith("llama-swap")]
    assert check.status == "ok"
    assert "no bench config" in check.detail


def _found(version):
    return lambda: version


def _missing():
    raise bench_doctor.LaunchError("not installed")


def _harnesses(checks):
    return [c for c in checks if c.subject.startswith("harness")]


def test_harness_found_is_ok_with_the_recorded_version_and_missing_is_skip(home):
    (home / "arms.yaml").write_text((DATA / "arms.yaml").read_text())
    versions = {"pi": _found("pi 1.2.3"), "claude-code": _missing}

    checks = _harnesses(bench_doctor.run_checks(None, base_url=_closed_url(), versions=versions))

    assert [(c.subject, c.status) for c in checks] == [
        ("harness pi", "ok"), ("harness claude-code", "skip"),
    ]
    assert checks[0].detail == "pi 1.2.3"


def test_harness_none_installed_fails(home):
    (home / "arms.yaml").write_text((DATA / "arms.yaml").read_text())
    versions = {"pi": _missing, "claude-code": _missing}

    checks = _harnesses(bench_doctor.run_checks(None, base_url=_closed_url(), versions=versions))

    assert [c.status for c in checks] == ["skip", "skip", "fail"]
    assert checks[-1].fix


def test_harness_without_a_config_checks_every_known_harness(home):
    versions = {"pi": _missing, "claude-code": _found("claude-code 2.0.1")}

    checks = _harnesses(bench_doctor.run_checks(None, base_url=_closed_url(), versions=versions))

    assert [(c.subject, c.status) for c in checks] == [
        ("harness pi", "skip"), ("harness claude-code", "ok"),
    ]


def test_harness_version_is_what_a_run_records(monkeypatch):
    monkeypatch.setattr(bench_doctor.launch_pi, "pi_version", lambda: "0.9.0")
    monkeypatch.setattr(bench_doctor.launch_cc, "find_claude", lambda: "/bin/claude")
    monkeypatch.setattr(bench_doctor.launch_cc, "claude_version", lambda path: "2.0.1")

    assert bench_doctor.HARNESS_VERSIONS["pi"]() == "pi 0.9.0"
    assert bench_doctor.HARNESS_VERSIONS["claude-code"]() == "claude-code 2.0.1"
