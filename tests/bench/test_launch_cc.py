"""The Claude Code launcher: dedicated config dir, run copy, env and argv, pins, hash, deny list.

The offline tests use no model. The deny-list probe runs the real claude binary
against a stub Anthropic Messages endpoint in this test, which asks for a Bash
call to `git push`; specflo's hook runs a stand-in `specflo` that only records
its call. The rig test does the same against the arm's llama-swap entry and is
run only with `-m rig`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from specflo.hook import settings_snippet

from modelbench import launch_cc, launch_pi

ENTRY = "swift15-flash-next-iq4xs-mtp-vision"
STRATA_ENTRY = "swift15-flash-next-iq4xs-strata-2x3090"
REAL_CLAUDE = Path.home() / ".local" / "bin" / "claude"
HAVE_CLAUDE = REAL_CLAUDE.exists()


def fake_claude(tmp_path: Path, version: str) -> str:
    """A stand-in binary that is not a script; `stub_version` answers its --version."""
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF stand-in " + version.encode())
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def stub_version(monkeypatch):
    """Answer `claude --version` without running a binary."""
    versions = {"value": "2.1.293"}
    monkeypatch.setattr(launch_cc, "claude_version", lambda _bin: versions["value"])
    return versions


@pytest.fixture
def launch(tmp_path: Path, stub_version) -> Iterator[launch_cc.CcLaunch]:
    workdir = tmp_path / "work"
    workdir.mkdir()
    with launch_cc.build_launch(
        ENTRY,
        workdir=workdir,
        run_dir=tmp_path / "run",
        claude_bin=fake_claude(tmp_path, "2.1.293"),
        base_env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/x",
            "ANTHROPIC_API_KEY": "sk-real",
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "CLAUDE_CODE_OAUTH_TOKEN": "real",
            "CLAUDECODE": "1",
            "CLAUDE_CONFIG_DIR": "/home/x/.claude",
        },
        args=["-p", "hello"],
    ) as launch:
        yield launch


# -- dedicated config dir -----------------------------------------------------


def test_config_dir_names_binary_and_base_url():
    frozen = launch_cc.load_frozen()
    assert frozen.claude_bin == str(REAL_CLAUDE)
    assert frozen.base_url == "http://localhost:8080"


def test_config_dir_holds_no_credential_hooks_or_state():
    names = {p.name for p in launch_cc.CONFIG_DIR.rglob("*")}
    for name in launch_cc.FORBIDDEN:
        assert name not in names
    settings = json.loads((launch_cc.CONFIG_DIR / "settings.json").read_text())
    assert "hooks" not in settings
    assert "env" not in settings


def test_deny_rules_are_the_pi_arm_rules():
    assert launch_cc.load_frozen().deny == launch_pi.load_frozen().deny
    assert launch_cc.deny_rules(["git push"]) == ["Bash(git push)", "Bash(git push *)"]


def test_config_dir_with_a_credential_is_refused(tmp_path: Path):
    config = tmp_path / "config"
    shutil.copytree(launch_cc.CONFIG_DIR, config)
    (config / ".credentials.json").write_text("{}\n")
    with pytest.raises(launch_pi.LaunchError, match=".credentials.json"):
        launch_cc.materialise(tmp_path / "run", ["git push"], config_dir=config)


def test_config_dir_with_hooks_is_refused(tmp_path: Path):
    config = tmp_path / "config"
    shutil.copytree(launch_cc.CONFIG_DIR, config)
    settings = json.loads((config / "settings.json").read_text())
    settings["hooks"] = {"Stop": []}
    (config / "settings.json").write_text(json.dumps(settings))
    with pytest.raises(launch_pi.LaunchError, match="hooks"):
        launch_cc.materialise(tmp_path / "run", ["git push"], config_dir=config)


# -- run copy -------------------------------------------------------------------


def test_run_copy_settings_carry_specflo_hook_deny_list_and_never_prompt_mode(launch):
    settings = json.loads((launch.config_dir / "settings.json").read_text())
    assert settings["hooks"] == settings_snippet()["hooks"]
    assert settings["permissions"]["defaultMode"] == "dontAsk"
    deny = settings["permissions"]["deny"]
    for rule in ("Bash(git push *)", "Bash(sudo *)", "Bash(rm -rf *)", "Bash(rm -r *)"):
        assert rule in deny
    assert "Bash" in settings["permissions"]["allow"]


def test_run_copy_state_file_skips_onboarding_and_trusts_the_workdir(launch):
    state = json.loads((launch.config_dir / ".claude.json").read_text())
    assert state["hasCompletedOnboarding"] is True
    assert state["projects"][str(launch.cwd)]["hasTrustDialogAccepted"] is True
    assert not (launch.config_dir / "claude.json").exists()


def test_run_copy_holds_no_credential(launch):
    names = {p.name for p in launch.config_dir.rglob("*")}
    assert ".credentials.json" not in names


def test_existing_run_copy_is_refused(launch):
    with pytest.raises(launch_pi.LaunchError, match="exists"):
        launch_cc.materialise(launch.config_dir.parent, ["git push"])


# -- env and argv -------------------------------------------------------------


def test_env_pins_config_dir_base_url_and_every_model_id(launch):
    env = launch.env
    assert env["CLAUDE_CONFIG_DIR"] == str(launch.config_dir)
    assert env["ANTHROPIC_BASE_URL"] == launch.shim.url
    assert launch.shim.upstream == "http://localhost:8080"
    for name in launch_cc.MODEL_VARS:
        assert env[name] == ENTRY, name
    for name in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                 "CLAUDE_CODE_SUBAGENT_MODEL"):
        assert name in launch_cc.MODEL_VARS
    assert env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"


def test_env_turns_off_nonessential_traffic_and_auto_update(launch):
    assert launch.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert launch.env["DISABLE_AUTOUPDATER"] == "1"


def test_env_drops_the_users_account_and_config(launch):
    env = launch.env
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDECODE" not in env
    assert env["ANTHROPIC_AUTH_TOKEN"] == launch_cc.DUMMY_TOKEN
    assert env["HOME"] == "/home/x"


def test_argv_runs_the_real_binary_with_never_prompt_mode(launch, tmp_path: Path):
    argv = launch.argv
    assert argv[0] == str(tmp_path / "bin" / "claude")
    assert argv[argv.index("--model") + 1] == ENTRY
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in argv
    assert argv[-2:] == ["-p", "hello"]
    assert launch.cwd == tmp_path / "work"


def test_wrapper_script_is_refused(tmp_path: Path, stub_version):
    wrapper = tmp_path / "claude"
    wrapper.write_text("#!/bin/bash\nexec ~/.local/bin/claude --append-system-prompt-file x \"$@\"\n")
    wrapper.chmod(0o755)
    with pytest.raises(launch_pi.LaunchError, match="wrapper"):
        launch_cc.build_launch(ENTRY, workdir=tmp_path, run_dir=tmp_path / "run",
                               claude_bin=str(wrapper))
    assert not (tmp_path / "run").exists()


def test_unknown_entry_is_refused(tmp_path: Path, stub_version):
    with pytest.raises(launch_pi.LaunchError, match="not-an-entry"):
        launch_cc.build_launch("not-an-entry", workdir=tmp_path, run_dir=tmp_path / "run",
                               claude_bin=fake_claude(tmp_path, "2.1.293"))


# -- version ------------------------------------------------------------------


def test_any_version_is_accepted_and_reported(tmp_path: Path, stub_version):
    stub_version["value"] = "2.1.294"
    workdir = tmp_path / "work"
    workdir.mkdir()
    with launch_cc.build_launch(ENTRY, workdir=workdir, run_dir=tmp_path / "run",
                                claude_bin=fake_claude(tmp_path, "2.1.294")) as launch:
        assert launch.report()["claude_version"] == "2.1.294"


def test_version_is_the_first_word_of_the_version_line(tmp_path: Path):
    script = tmp_path / "v.sh"
    script.write_text("#!/bin/sh\necho '2.1.293 (Claude Code)'\n")
    script.chmod(0o755)
    assert launch_cc.claude_version(str(script)) == "2.1.293"


@pytest.mark.skipif(not HAVE_CLAUDE, reason="the real claude binary is not installed")
def test_installed_claude_is_the_real_binary_and_reports_a_version():
    launch_cc.check_real_binary(str(REAL_CLAUDE))
    assert launch_cc.claude_version(str(REAL_CLAUDE))


# -- hash and record fields ---------------------------------------------------


def test_hash_is_stable_across_run_copies(tmp_path: Path, launch):
    other = launch_cc.materialise(tmp_path / "run2", launch_cc.load_frozen().deny)
    assert launch_pi.tree_hash(other) == launch.config_hash
    assert launch.config_hash.startswith("sha256:")


def test_hash_changes_with_config_content(tmp_path: Path, launch):
    config = tmp_path / "config"
    shutil.copytree(launch_cc.CONFIG_DIR, config)
    settings = json.loads((config / "settings.json").read_text())
    settings["permissions"]["allow"].append("WebFetch")
    (config / "settings.json").write_text(json.dumps(settings))
    changed = launch_cc.materialise(tmp_path / "run3", launch_cc.load_frozen().deny, config)
    assert launch_pi.tree_hash(changed) != launch.config_hash


def test_report_carries_version_and_config_hash(launch):
    report = launch.report()
    assert report["harness"] == "claude-code 2.1.293"
    assert report["claude_version"] == "2.1.293"
    assert report["config_hash"] == launch.config_hash
    assert report["request_shim"] == "late-system-to-user"


# -- request shim ---------------------------------------------------------------


def test_strata_entry_talks_to_llama_swap_directly(tmp_path: Path, stub_version):
    workdir = tmp_path / "work"
    workdir.mkdir()
    with launch_cc.build_launch(STRATA_ENTRY, workdir=workdir, run_dir=tmp_path / "run",
                                claude_bin=fake_claude(tmp_path, "2.1.293")) as launch:
        assert launch.shim is None
        assert launch.env["ANTHROPIC_BASE_URL"] == "http://localhost:8080"
        assert launch.report()["request_shim"] == "none"


# -- deny list inside the real claude, against a stub Anthropic endpoint --------


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class AnthropicStub:
    """A Messages endpoint. Main-conversation requests (those offering Bash) walk
    `steps` in order; any other request (a background call) gets plain text."""

    def __init__(self, steps: list[dict]) -> None:
        self.steps = steps
        self.requests: list[dict] = []
        self.main = 0
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                if "count_tokens" in self.path:
                    return self._json({"input_tokens": 100})
                if not self.path.startswith("/v1/messages"):
                    self.send_response(404)
                    self.end_headers()
                    return
                message = stub.reply(body)
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(stub.stream(message).encode())
                else:
                    self._json(message)

            def _json(self, obj: dict) -> None:
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def reply(self, body: dict) -> dict:
        with self.lock:
            self.requests.append(body)
            step: dict = {"text": "ok"}
            if "Bash" in [t.get("name") for t in body.get("tools") or []]:
                step = self.steps[min(self.main, len(self.steps) - 1)]
                self.main += 1
            n = len(self.requests)
        if "bash" in step:
            content = [{"type": "tool_use", "id": f"toolu_{n:04d}", "name": "Bash",
                        "input": {"command": step["bash"]}}]
        else:
            content = [{"type": "text", "text": step["text"]}]
        return {
            "id": f"msg_{n:04d}", "type": "message", "role": "assistant",
            "model": body.get("model"), "content": content,
            "stop_reason": "tool_use" if "bash" in step else "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    @staticmethod
    def stream(message: dict) -> str:
        block = message["content"][0]
        start = {**message, "content": [], "stop_reason": None}
        out = _sse("message_start", {"type": "message_start", "message": start})
        if block["type"] == "tool_use":
            first, delta = {**block, "input": {}}, {"type": "input_json_delta",
                                                    "partial_json": json.dumps(block["input"])}
        else:
            first, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": block["text"]}
        out += _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": first})
        out += _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta})
        out += _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        out += _sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": 5},
                                      "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None}})
        return out + _sse("message_stop", {"type": "message_stop"})

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _recording_specflo(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in `specflo` on PATH that records each call; returns (bin dir, record file)."""
    bindir, calls = tmp_path / "fakebin", tmp_path / "specflo-calls"
    bindir.mkdir()
    script = bindir / "specflo"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> '{calls}'\n")
    script.chmod(0o755)
    return bindir, calls


def _run(launch: launch_cc.CcLaunch, timeout: float) -> tuple[int, list[dict], str]:
    proc = launch_cc.start(launch, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(f"claude did not exit in {timeout} s (a prompt would hang it): {err[-2000:]}")
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return proc.returncode, events, err


def _tool_results(events: list[dict]) -> list[dict]:
    return [block for e in events if e.get("type") == "user"
            for block in (e.get("message", {}).get("content") or [])
            if isinstance(block, dict) and block.get("type") == "tool_result"]


@pytest.mark.skipif(not HAVE_CLAUDE, reason="the real claude binary is not installed")
def test_real_claude_blocks_git_push_with_no_prompt(tmp_path: Path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    bindir, calls = _recording_specflo(tmp_path)
    outside = tmp_path / "outside"
    (outside / "keep").mkdir(parents=True)
    denied = ["git push origin main", "cd . && git push", "sudo -n true", f"rm -rf {outside}"]
    stub = AnthropicStub([
        {"bash": "printenv CLAUDE_CONFIG_DIR ANTHROPIC_BASE_URL ANTHROPIC_MODEL"},
        *({"bash": command} for command in denied),
        {"text": "done"},
    ])
    try:
        launch = launch_cc.build_launch(
            ENTRY, workdir=workdir, run_dir=tmp_path / "run", base_url=stub.base_url,
            extra_env={"PATH": f"{bindir}:{os.environ['PATH']}"},
            args=["-p", "--output-format", "stream-json", "--verbose",
                  "--no-session-persistence", "probe the deny list"],
        )
        code, events, err = _run(launch, 90)
        launch.close()
    finally:
        stub.close()

    assert code == 0, err
    assert stub.requests and {r.get("model") for r in stub.requests} == {ENTRY}
    results = _tool_results(events)
    assert len(results) == 1 + len(denied), results
    env_lines = str(results[0]["content"]).splitlines()
    assert env_lines == [str(launch.config_dir), launch.shim.url, ENTRY]
    # The shim turned Claude Code's late system messages into user messages.
    assert not [m for r in stub.requests for m in r.get("messages", []) if m.get("role") == "system"]
    for command, result in zip(denied, results[1:]):
        assert result["is_error"] is True, command
        assert "has been denied" in str(result["content"]), (command, result)
    denials = [e for e in events if e.get("subtype") == "permission_denied"]
    assert len(denials) == len(denied)
    assert (outside / "keep").is_dir()
    final = [e for e in events if e.get("type") == "result"][-1]
    assert final.get("is_error") is False
    assert "hook reseed --format claude" in calls.read_text()


# -- rig: the arm's llama-swap entry -------------------------------------------


@pytest.mark.rig
@pytest.mark.timeout(1200)
def test_rig_claude_blocks_git_push_with_no_prompt(tmp_path: Path):
    """A real model through llama-swap asks for `git push` and gets a denial."""
    from modelbench import arms, preflight

    run = arms.Run(entry=ENTRY, harness="claude-code", level="quick", run_index=0)
    # Refuses when llama-swap holds a model the bench did not load. When the
    # entry is not loaded yet, Claude Code's request loads it, so the bench
    # records it.
    running = preflight.preflight(run)
    if ENTRY not in running:
        preflight.record_bench_load(preflight.DEFAULT_STATE, ENTRY)

    workdir = tmp_path / "work"
    workdir.mkdir()
    bindir, _calls = _recording_specflo(tmp_path)
    with launch_cc.build_launch(
        ENTRY, workdir=workdir, run_dir=tmp_path / "run",
        extra_env={"PATH": f"{bindir}:{os.environ['PATH']}"},
        args=["-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
              "Use the Bash tool to run exactly this command and nothing else: "
              "git push origin main. Then reply with the tool's output."],
    ) as launch:
        code, events, err = _run(launch, 900)

    assert code == 0, err
    models = {e["message"].get("model") for e in events if e.get("type") == "assistant"}
    assert models == {ENTRY}
    denials = [e for e in events if e.get("subtype") == "permission_denied"]
    assert denials, "the model made no denied Bash call"
    assert "git push" in denials[0]["message"]
