"""The pi launcher: frozen config, run copy, env and argv, version pin, hash, deny list.

The offline tests use no model. The deny-list probes run the real extension
file two ways: under node against a fake pi (the hook called directly), and
inside a real pi process whose "model" is a stub OpenAI endpoint in this test,
which asks for a bash call to `git push`. The rig test does the same against
the arm's llama-swap entry and is run only with `-m rig`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from modelbench import arms, launch_pi

ENTRY = "swift15-flash-next-iq4xs-mtp-vision"
HAVE_PI = shutil.which("pi") is not None
HAVE_NODE = shutil.which("node") is not None


def fake_pi(tmp_path: Path, version: str) -> str:
    """A stand-in pi binary that only answers --version."""
    path = tmp_path / "bin" / "pi"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{version}'\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def launch(tmp_path: Path) -> launch_pi.PiLaunch:
    workdir = tmp_path / "work"
    workdir.mkdir()
    return launch_pi.build_launch(
        ENTRY,
        workdir=workdir,
        run_dir=tmp_path / "run",
        pi_bin=fake_pi(tmp_path, "1.0.4"),
        base_env={"PATH": "/usr/bin:/bin", "HOME": "/home/x", "SPECFLO_POOL_DENY": "[]"},
        args=["--mode", "json", "hello"],
    )


# -- frozen directory ---------------------------------------------------------


def test_frozen_dir_pins_version_and_provider():
    frozen = launch_pi.load_frozen()
    assert frozen.pi_version == "1.0.4"
    assert frozen.provider == "llama-swap"


def test_frozen_dir_holds_no_credential_and_no_extensions():
    names = {p.name for p in launch_pi.FROZEN_DIR.rglob("*")}
    assert "auth.json" not in names
    assert not (launch_pi.FROZEN_DIR / "extensions").exists()
    assert not (launch_pi.FROZEN_DIR / "sessions").exists()


def test_frozen_models_point_at_llama_swap_and_declare_every_entry():
    models = json.loads((launch_pi.FROZEN_DIR / "models.json").read_text())
    provider = models["providers"]["llama-swap"]
    assert provider["baseUrl"] == "http://localhost:8080/v1"
    assert provider["authHeader"] is False
    ids = {m["id"] for m in provider["models"]}
    assert ids == set(arms.load_config().entries)


def test_frozen_settings_load_no_packages_and_never_trust_a_project():
    settings = json.loads((launch_pi.FROZEN_DIR / "settings.json").read_text())
    assert settings["packages"] == []
    assert settings["extensions"] == []
    assert settings["defaultProjectTrust"] == "never"


def test_deny_list_covers_push_sudo_and_recursive_delete():
    deny = launch_pi.load_frozen().deny
    assert {"git push", "sudo", "rm -rf", "rm -r"} <= set(deny)


def test_frozen_dir_with_extensions_is_refused(tmp_path: Path):
    frozen = tmp_path / "frozen"
    shutil.copytree(launch_pi.FROZEN_DIR, frozen)
    (frozen / "extensions").mkdir()
    (frozen / "extensions" / "extra.ts").write_text("export default () => {}\n")
    with pytest.raises(launch_pi.LaunchError, match="extensions"):
        launch_pi.materialise(tmp_path / "run", frozen_dir=frozen)


def test_frozen_dir_with_a_credential_is_refused(tmp_path: Path):
    frozen = tmp_path / "frozen"
    shutil.copytree(launch_pi.FROZEN_DIR, frozen)
    (frozen / "auth.json").write_text("{}\n")
    with pytest.raises(launch_pi.LaunchError, match="auth.json"):
        launch_pi.materialise(tmp_path / "run", frozen_dir=frozen)


# -- run copy and extension set ----------------------------------------------


def test_run_copy_extensions_are_exactly_specflo_and_the_deny_list(launch):
    assert launch_pi.extension_names(launch.agent_dir) == sorted(launch_pi.EXTENSIONS)
    ext = launch.agent_dir / "extensions"
    assert (ext / "specflo" / "package.json").is_file()
    assert (ext / "specflo" / "src" / "index.ts").is_file()
    assert not (ext / "specflo" / "test").exists()
    deny_src = launch_pi.DENY_SOURCE.read_bytes()
    assert (ext / launch_pi.DENY_EXTENSION).read_bytes() == deny_src


def test_run_copy_declares_only_the_run_entry_and_makes_it_the_default(launch):
    models = json.loads((launch.agent_dir / launch_pi.MODELS_FILE).read_text())
    provider = launch_pi.load_frozen().provider
    assert [m["id"] for m in models["providers"][provider]["models"]] == [ENTRY]
    settings = json.loads((launch.agent_dir / "settings.json").read_text())
    assert (settings["defaultProvider"], settings["defaultModel"]) == (provider, ENTRY)
    assert len(launch_pi.model_ids(launch_pi.FROZEN_DIR, provider)) > 1


def test_run_copy_is_outside_the_frozen_dir(launch):
    assert launch_pi.FROZEN_DIR not in launch.agent_dir.parents
    assert launch.agent_dir != launch_pi.FROZEN_DIR


def test_existing_run_copy_is_refused(tmp_path: Path, launch):
    with pytest.raises(launch_pi.LaunchError, match="exists"):
        launch_pi.materialise(launch.agent_dir.parent)


# -- env and argv -------------------------------------------------------------


def test_env_points_pi_at_the_run_copy_with_the_frozen_deny_list(launch):
    env = launch.env
    assert env["PI_CODING_AGENT_DIR"] == str(launch.agent_dir)
    assert json.loads(env["SPECFLO_POOL_DENY"]) == list(launch_pi.load_frozen().deny)
    assert env["PI_OFFLINE"] == "1"
    assert env["PI_SKIP_VERSION_CHECK"] == "1"
    assert env["PI_TELEMETRY"] == "0"
    assert env["HOME"] == "/home/x"
    assert "PI_CODING_AGENT_SESSION_DIR" not in env


def test_argv_runs_the_arm_entry_with_no_approve(launch, tmp_path: Path):
    argv = launch.argv
    assert argv[0] == str(tmp_path / "bin" / "pi")
    assert "--no-approve" in argv
    assert "--no-skills" in argv
    assert "--no-extensions" in argv
    loaded = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert loaded == [str(launch.agent_dir / "extensions" / n) for n in launch_pi.EXTENSIONS]
    assert argv[argv.index("--provider") + 1] == "llama-swap"
    assert argv[argv.index("--model") + 1] == ENTRY
    assert argv[-3:] == ["--mode", "json", "hello"]
    assert launch.cwd == tmp_path / "work"


def test_unknown_entry_is_refused(tmp_path: Path):
    with pytest.raises(launch_pi.LaunchError, match="not-an-entry"):
        launch_pi.build_launch(
            "not-an-entry",
            workdir=tmp_path,
            run_dir=tmp_path / "run",
            pi_bin=fake_pi(tmp_path, "1.0.4"),
        )
    assert not (tmp_path / "run").exists()


def test_workdir_inside_the_frozen_dir_is_refused(tmp_path: Path):
    with pytest.raises(launch_pi.LaunchError, match="workdir"):
        launch_pi.build_launch(
            ENTRY,
            workdir=launch_pi.FROZEN_DIR,
            run_dir=tmp_path / "run",
            pi_bin=fake_pi(tmp_path, "1.0.4"),
        )


# -- version pin --------------------------------------------------------------


def test_version_mismatch_is_refused_before_any_copy(tmp_path: Path):
    with pytest.raises(launch_pi.LaunchError, match="1.0.5.*1.0.4|1.0.4.*1.0.5"):
        launch_pi.build_launch(
            ENTRY,
            workdir=tmp_path,
            run_dir=tmp_path / "run",
            pi_bin=fake_pi(tmp_path, "1.0.5"),
        )
    assert not (tmp_path / "run").exists()


def test_missing_pi_binary_is_refused(tmp_path: Path):
    with pytest.raises(launch_pi.LaunchError, match="pi"):
        launch_pi.pi_version(str(tmp_path / "no-such-pi"))


@pytest.mark.skipif(not HAVE_PI, reason="pi is not installed")
def test_installed_pi_matches_the_pin():
    assert launch_pi.pi_version() == launch_pi.load_frozen().pi_version


# -- hash and record fields ---------------------------------------------------


def test_hash_is_stable_across_run_copies(tmp_path: Path, launch):
    other = launch_pi.materialise(tmp_path / "run2")
    assert launch_pi.tree_hash(other) == launch.config_hash
    assert launch.config_hash.startswith("sha256:")


def test_hash_is_the_same_for_every_entry(tmp_path: Path, launch):
    provider = launch_pi.load_frozen().provider
    other_entry = next(e for e in launch_pi.model_ids(launch_pi.FROZEN_DIR, provider) if e != ENTRY)
    workdir = tmp_path / "work2"
    workdir.mkdir()
    other = launch_pi.build_launch(other_entry, workdir=workdir, run_dir=tmp_path / "run4",
                                   pi_bin=fake_pi(tmp_path, "1.0.4"), base_env={"PATH": "/usr/bin:/bin"})
    assert other.config_hash == launch.config_hash


def test_hash_is_taken_before_pi_writes_into_the_run_copy(launch):
    (launch.agent_dir / "sessions").mkdir()
    (launch.agent_dir / "sessions" / "s.jsonl").write_text("{}\n")
    assert launch_pi.tree_hash(launch.agent_dir) != launch.config_hash


def test_hash_changes_with_frozen_content(tmp_path: Path, launch):
    frozen = tmp_path / "frozen"
    shutil.copytree(launch_pi.FROZEN_DIR, frozen)
    settings = json.loads((frozen / "settings.json").read_text())
    settings["defaultThinkingLevel"] = "low"
    (frozen / "settings.json").write_text(json.dumps(settings))
    changed = launch_pi.materialise(tmp_path / "run3", frozen_dir=frozen)
    assert launch_pi.tree_hash(changed) != launch.config_hash


def test_report_carries_pi_version_and_config_hash(launch):
    report = launch.report()
    assert report["harness"] == "pi 1.0.4"
    assert report["pi_version"] == "1.0.4"
    assert report["config_hash"] == launch.config_hash
    assert report["agent_dir"] == str(launch.agent_dir)


# -- deny list against a fake pi (the hook called directly) --------------------

_NODE_PROBE = """
const [extPath, commandsJson] = process.argv.slice(1);
const mod = await import(new URL("file://" + extPath).href);
const handlers = [];
mod.default({ on: (event, handler) => { if (event === "tool_call") handlers.push(handler); } });
const out = {};
for (const command of JSON.parse(commandsJson)) {
  let result = null;
  for (const handler of handlers) {
    const r = await handler({ type: "tool_call", toolCallId: "c1", toolName: "bash", input: { command } }, {});
    if (r && r.block) { result = r; break; }
  }
  out[command] = result;
}
console.log(JSON.stringify(out));
"""

BLOCKED = [
    "git push origin main",
    "cd sub && git push",
    "sudo rm /etc/hosts",
    "rm -rf ~/AI",
    "rm -rf /tmp/elsewhere",
    "rm -r ../other",
    "rm -fr $HOME/.cache",
    "rm --recursive /",
]
ALLOWED = ["git status", "ls -la", "uv run pytest", "rm notes.txt", "git log --grep push"]


@pytest.mark.skipif(not HAVE_NODE, reason="node is not installed")
def test_deny_hook_blocks_with_no_prompt(launch):
    ext = launch.agent_dir / "extensions" / launch_pi.DENY_EXTENSION
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", _NODE_PROBE, str(ext), json.dumps(BLOCKED + ALLOWED)],
        env={**launch.env, "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    for command in BLOCKED:
        assert results[command] and results[command]["block"] is True, command
        assert "deny list" in results[command]["reason"]
    for command in ALLOWED:
        assert results[command] is None, command


# -- deny list inside a real pi, against a stub model -------------------------


class _Stub:
    """An OpenAI chat-completions endpoint that replays scripted turns."""

    def __init__(self, turns: list[dict]) -> None:
        self.turns = turns
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                if "chat/completions" not in self.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                stub.requests.append(json.loads(body or b"{}"))
                turn = stub.turns[min(len(stub.requests) - 1, len(stub.turns) - 1)]
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                self.wfile.write(stub.stream(turn, len(stub.requests)).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    @staticmethod
    def stream(turn: dict, n: int) -> str:
        def chunk(delta: dict, finish: str | None) -> str:
            payload = {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": ENTRY,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload)}\n\n"

        out = chunk({"role": "assistant", "content": ""}, None)
        if "bash" in turn:
            call = {
                "index": 0,
                "id": f"call_{n}",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": turn["bash"]})},
            }
            out += chunk({"tool_calls": [call]}, None) + chunk({}, "tool_calls")
        else:
            out += chunk({"content": turn.get("text", "ok")}, None) + chunk({}, "stop")
        return out + "data: [DONE]\n\n"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _tool_texts(request: dict) -> list[str]:
    texts = []
    for message in request.get("messages", []):
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        texts.append(str(content))
    return texts


def _point_at(launch: launch_pi.PiLaunch, base_url: str) -> None:
    """Point the run copy (never the frozen dir) at a stub, after the hash."""
    models_path = launch.agent_dir / "models.json"
    models = json.loads(models_path.read_text())
    models["providers"]["llama-swap"]["baseUrl"] = base_url
    models_path.write_text(json.dumps(models))


# Hermetic: the specflo extension shells out to a no-op instead of the specflo
# CLI, and its control server stays off.
_HERMETIC = {"SPECFLO_BIN": "true", "SPECFLO_AGENT_SERVE": "0"}


@pytest.mark.skipif(not HAVE_PI, reason="pi is not installed")
def test_real_pi_loads_only_the_run_copy_extensions(tmp_path: Path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    stub = _Stub([{"text": "ok"}])
    try:
        launch = launch_pi.build_launch(
            ENTRY, workdir=workdir, run_dir=tmp_path / "run",
            args=["--mode", "rpc", "--no-session"], extra_env=_HERMETIC,
        )
        _point_at(launch, stub.base_url)
        proc = launch_pi.start(launch, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
        out, err = proc.communicate(json.dumps({"type": "get_commands", "id": "1"}) + "\n",
                                    timeout=60)
    finally:
        stub.close()
    responses = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
    reply = next(r for r in responses if r.get("id") == "1")
    commands = [c for c in reply["data"]["commands"] if c["source"] == "extension"]
    paths = {c["sourceInfo"]["path"] for c in commands}
    assert paths, err
    ext_dir = str(launch.agent_dir / "extensions")
    assert all(p.startswith(ext_dir) for p in paths), paths
    assert any(c["name"] == "specflo-continue" for c in commands)


@pytest.mark.skipif(not HAVE_PI, reason="pi is not installed")
def test_real_pi_blocks_git_push_with_no_prompt(tmp_path: Path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    stub = _Stub([
        {"bash": "printenv PI_CODING_AGENT_DIR"},
        {"bash": "git push origin main"},
        {"text": "done"},
    ])
    try:
        launch = launch_pi.build_launch(
            ENTRY,
            workdir=workdir,
            run_dir=tmp_path / "run",
            args=["--mode", "json", "--no-session", "probe the deny list"],
            extra_env=_HERMETIC,
        )
        _point_at(launch, stub.base_url)
        proc = launch_pi.start(launch, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
        out, err = proc.communicate(timeout=90)
    finally:
        stub.close()

    assert proc.returncode == 0, err
    assert len(stub.requests) == 3, err
    assert all(r.get("model") == ENTRY for r in stub.requests)
    assert _tool_texts(stub.requests[1])[-1].strip() == str(launch.agent_dir)
    blocked = _tool_texts(stub.requests[2])[-1]
    assert "Blocked by the deny list" in blocked and "'git push'" in blocked
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert not [e for e in events if e.get("type") == "extension_ui_request"]
    ends = [e for e in events if e.get("type") == "tool_execution_end"]
    assert ends and ends[-1]["isError"] is True


# -- rig: the arm's llama-swap entry -------------------------------------------


@pytest.mark.rig
@pytest.mark.timeout(1200)
def test_rig_pi_blocks_git_push_with_no_prompt(tmp_path: Path):
    """A real model through llama-swap asks for `git push` and gets a block."""
    from modelbench import preflight

    run = arms.Run(entry=ENTRY, harness="pi", level="quick", run_index=0)
    # Refuses when llama-swap holds a model the bench did not load. When the
    # entry is not loaded yet, pi's request loads it, so the bench records it.
    running = preflight.preflight(run)
    if ENTRY not in running:
        preflight.record_bench_load(preflight.DEFAULT_STATE, ENTRY)

    workdir = tmp_path / "work"
    workdir.mkdir()
    launch = launch_pi.build_launch(
        ENTRY,
        workdir=workdir,
        run_dir=tmp_path / "run",
        args=[
            "--mode", "json", "--no-session",
            "Use the bash tool to run exactly this command and nothing else: "
            "git push origin main. Then reply with the tool's output.",
        ],
        extra_env=_HERMETIC,
    )
    proc = launch_pi.start(launch, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
    out, err = proc.communicate(timeout=900)

    assert proc.returncode == 0, err
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert not [e for e in events if e.get("type") == "extension_ui_request"]
    ends = [e for e in events if e.get("type") == "tool_execution_end" and e.get("toolName") == "bash"]
    assert ends, "the model made no bash call"
    text = json.dumps(ends[0]["result"])
    assert ends[0]["isError"] is True and "Blocked by the deny list" in text, text
