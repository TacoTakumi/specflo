import json

from typer.testing import CliRunner

from specflo import checkpoint, config, hook, projects
from specflo.cli import app

runner = CliRunner()


def _active(tmp_path, name="My Thing"):
    """Initialize a repo with one active project; return its cfg."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, name)
    projects.switch_project(tmp_path, cfg, name)  # sets active + persists to disk
    return cfg


def test_reseed_text_active_leads_with_directive_then_checkpoint(tmp_path):
    cfg = _active(tmp_path)
    out = hook.reseed_text(tmp_path)

    # leads with the confirmation-gate directive (do-not-start-work + ask)
    assert out.startswith(hook.CONFIRMATION_DIRECTIVE)
    lowered = hook.CONFIRMATION_DIRECTIVE.lower()
    assert "not begin work" in lowered  # do-not-start-work
    assert "ask" in lowered             # present-checkpoint-and-ask

    # contains the byte-exact checkpoint render, positioned after the directive
    project = projects.load_project(tmp_path, cfg, "my-thing")
    body = checkpoint.render_checkpoint(checkpoint.build_checkpoint(tmp_path, project))
    assert body in out
    assert out.index(hook.CONFIRMATION_DIRECTIVE) < out.index(body)


def test_reseed_text_noop_outside_specflo_repo(tmp_path):
    # no .specflo anywhere up the tree -> nothing to emit, no raise
    assert hook.reseed_text(tmp_path) == ""


def test_reseed_text_noop_no_active_project(tmp_path):
    config.init_config(tmp_path)  # initialized, but no project created/activated
    assert hook.reseed_text(tmp_path) == ""


def test_reseed_text_noop_corrupt_active_project(tmp_path):
    _active(tmp_path)
    (tmp_path / "docs" / "projects" / "my-thing" / "project.md").unlink()  # unreadable
    assert hook.reseed_text(tmp_path) == ""


# --- the `specflo hook reseed` CLI command ------------------------------


def test_cli_hook_reseed_active_project_prints_payload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["hook", "reseed"])
    assert result.exit_code == 0
    assert hook.CONFIRMATION_DIRECTIVE in result.output
    assert "## Do next" in result.output  # the checkpoint body came through


def test_cli_hook_reseed_no_active_project_is_silent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])  # initialized, but no active project
    result = runner.invoke(app, ["hook", "reseed"])
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_cli_hook_reseed_does_not_block_on_stdin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["hook", "reseed"], input="")  # closed/empty stdin
    assert result.exit_code == 0
    assert hook.CONFIRMATION_DIRECTIVE in result.output


# --- the Claude SessionStart JSON (`hook reseed --format claude`) --------
# Claude Code can't make the agent take a turn after startup/clear/resume, so the
# wiring emits structured JSON: `additionalContext` re-grounds the agent, and a
# user-visible `systemMessage` tells the human what to type to kick it off.


def test_claude_session_start_output_wraps_context_and_nudges_user(tmp_path):
    _active(tmp_path)
    payload = json.loads(hook.claude_session_start_output(tmp_path))

    # additionalContext carries the verbatim reseed payload — for the agent
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    assert hso["additionalContext"] == hook.reseed_text(tmp_path)

    # systemMessage is shown to the *user* and tells them what to type
    msg = payload["systemMessage"]
    assert "continue" in msg.lower()   # the concrete thing to type
    assert "my-thing" in msg           # names the active project


def test_claude_session_start_output_noop_no_active_project(tmp_path):
    config.init_config(tmp_path)       # initialized, no active project
    assert hook.claude_session_start_output(tmp_path) == ""


def test_claude_session_start_output_noop_outside_repo(tmp_path):
    assert hook.claude_session_start_output(tmp_path) == ""


def test_cli_hook_reseed_format_claude_emits_json_with_systemmessage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["hook", "reseed", "--format", "claude"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "continue" in payload["systemMessage"].lower()


def test_cli_hook_reseed_format_claude_silent_no_active_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["hook", "reseed", "--format", "claude"])
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_cli_hook_reseed_default_format_is_plain_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["hook", "reseed"])  # default: portable text, not JSON
    assert result.exit_code == 0
    assert result.output.startswith(hook.CONFIRMATION_DIRECTIVE)


# --- `specflo hook print` (the SessionStart wiring) ---------------------


def test_hook_print_settings_snippet_shape():
    snip = hook.settings_snippet()
    entries = snip["hooks"]["SessionStart"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["matcher"] == "startup|clear|resume"   # clear + startup + resume
    assert "compact" not in entry["matcher"]            # compact still excluded
    assert entry["hooks"] == [
        {"type": "command", "command": "specflo hook reseed --format claude"}
    ]


def test_hook_print_cli_emits_parseable_wiring(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["hook", "print"])
    assert result.exit_code == 0
    entry = json.loads(result.output)["hooks"]["SessionStart"][0]
    assert entry["matcher"] == "startup|clear|resume"
    assert entry["hooks"][0]["command"] == "specflo hook reseed --format claude"


# --- `specflo hook print --install` (idempotent settings.json merge) -----


def _reseed_entries(settings: dict) -> list:
    return [
        e for e in settings.get("hooks", {}).get("SessionStart", [])
        if e.get("matcher") == hook.RESEED_MATCHER
        and any(h.get("command") == hook.RESEED_COMMAND for h in e.get("hooks", []))
    ]


def test_hook_install_creates_settings_when_absent(tmp_path):
    path = hook.install_hook(tmp_path)
    assert path == tmp_path / ".claude" / "settings.json"
    data = json.loads(path.read_text())
    assert len(_reseed_entries(data)) == 1


def test_hook_install_preserves_existing_settings(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"SessionStart": [
            {"matcher": "resume", "hooks": [{"type": "command", "command": "echo hi"}]}
        ]},
    }))
    hook.install_hook(tmp_path)
    data = json.loads((claude / "settings.json").read_text())
    assert data["permissions"] == {"allow": ["Bash(ls)"]}            # untouched
    matchers = [e.get("matcher") for e in data["hooks"]["SessionStart"]]
    assert "resume" in matchers                                      # pre-existing kept
    assert len(_reseed_entries(data)) == 1                           # ours added


def test_hook_install_is_idempotent(tmp_path):
    hook.install_hook(tmp_path)
    hook.install_hook(tmp_path)
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(_reseed_entries(data)) == 1                           # no duplicate


def test_hook_install_migrates_old_reseed_wiring(tmp_path):
    # a previously-installed (pre-resume, plain-text) reseed entry is rewired in
    # place to the current matcher + command, not left stale and not duplicated.
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "startup|clear",
         "hooks": [{"type": "command", "command": "specflo hook reseed"}]}
    ]}}))
    hook.install_hook(tmp_path)
    data = json.loads((claude / "settings.json").read_text())
    assert len(_reseed_entries(data)) == 1                           # migrated, not dup'd
    cmds = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    assert cmds == ["specflo hook reseed --format claude"]           # old form replaced


def test_hook_install_cli_writes_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["hook", "print", "--install"])
    assert result.exit_code == 0
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(_reseed_entries(data)) == 1


def test_hook_install_tolerates_wrong_shaped_settings(tmp_path):
    # hand-edited / malformed shapes must not raise; install still wires ours
    claude = tmp_path / ".claude"
    claude.mkdir()
    for bad in ('{"hooks": []}',
                '{"hooks": {"SessionStart": {"matcher": "x"}}}',
                '{"hooks": "weird"}'):
        (claude / "settings.json").write_text(bad)
        hook.install_hook(tmp_path)  # must not raise
        data = json.loads((claude / "settings.json").read_text())
        assert len(_reseed_entries(data)) == 1


def test_reseed_text_defaults_to_cwd(tmp_path, monkeypatch):
    # cwd resolution lives inside the never-errors guard; no-arg call works
    _active(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert hook.reseed_text().startswith(hook.CONFIRMATION_DIRECTIVE)
