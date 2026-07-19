"""Tests for `specflo auto` and the auto-mode handoff payload (`auto.py`).

T-01 lays down the skeleton: a `specflo auto` command and an `auto.py` that
emits an auto-mode *bootstrap* section for the current phase. It is strictly
additive (REQ-02) and harness-neutral (REQ-05) - the source-scan tests below
pin those invariants so later tasks can't quietly break them.
"""

import io
import tokenize
from pathlib import Path

from typer.testing import CliRunner

from specflo import auto, config, projects
from specflo.cli import app

runner = CliRunner()

PHASES = ["brainstorm", "spec", "plan", "execute"]


def _active_at(tmp_path, phase, name="My Thing"):
    """Init a repo with one active project advanced to ``phase``; return its slug."""
    cfg = config.init_config(tmp_path)
    projects.create_project(tmp_path, cfg, name)
    projects.switch_project(tmp_path, cfg, name)  # sets active + persists to disk
    slug = "my-thing"
    for _ in range(PHASES.index(phase)):  # advance the phase pointer (bypasses gates)
        projects.advance_project(tmp_path, cfg, slug)
    return slug


# --- payload derivation -------------------------------------------------------

def test_auto_text_emits_bootstrap_at_each_phase(tmp_path):
    # invoked at any of the four phases, the payload carries the bootstrap marker
    # and names the phase it is bootstrapping.
    for phase in PHASES:
        subdir = tmp_path / phase
        subdir.mkdir()
        _active_at(subdir, phase)
        out = auto.auto_text(subdir)
        assert auto.BOOTSTRAP_MARKER in out, f"no bootstrap section at {phase}"
        assert phase in out, f"payload does not name the {phase} phase"


def test_auto_bootstrap_names_phase_and_marker():
    # the bootstrap builder is a pure function of the phase and always leads with
    # the fixed marker (structural, not verbatim, contract for later tasks).
    for phase in PHASES:
        block = auto.auto_bootstrap(phase)
        assert block.startswith(auto.BOOTSTRAP_MARKER)
        assert phase in block


def test_auto_text_noop_outside_specflo_repo(tmp_path):
    # no .specflo anywhere up the tree -> nothing to emit, and never raises.
    assert auto.auto_text(tmp_path) == ""


def test_auto_text_noop_no_active_project(tmp_path):
    config.init_config(tmp_path)  # initialized, but no project created/activated
    assert auto.auto_text(tmp_path) == ""


def test_auto_text_defaults_to_cwd(tmp_path, monkeypatch):
    _active_at(tmp_path, "brainstorm")
    monkeypatch.chdir(tmp_path)
    assert auto.BOOTSTRAP_MARKER in auto.auto_text()  # no-arg call resolves cwd


# --- CLI surface --------------------------------------------------------------

def test_auto_appears_in_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "auto" in result.stdout


def test_cli_auto_prints_bootstrap_and_exits_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    runner.invoke(app, ["new", "My Thing"])
    result = runner.invoke(app, ["auto"])
    assert result.exit_code == 0
    assert auto.BOOTSTRAP_MARKER in result.stdout


# --- invariants: strictly additive (REQ-02), harness-neutral (REQ-05) ---------

_AUTO_SRC = Path(auto.__file__).read_text()


def _auto_code_only() -> str:
    """`auto.py` source with comments and string literals (incl. docstrings) removed.

    Used to assert the *code* never touches a symbol while prose is free to
    explain the invariant.
    """
    tokens = tokenize.generate_tokens(io.StringIO(_AUTO_SRC).readline)
    return " ".join(
        tok.string
        for tok in tokens
        if tok.type not in (tokenize.COMMENT, tokenize.STRING)
    )


def test_auto_source_spawns_no_nested_agent_or_session():
    # REQ-05: specflo emits the payload and launches no nested agent/session.
    for banned in ("subprocess", "os.system", "Popen", "os.exec"):
        assert banned not in _AUTO_SRC, f"auto.py must not reference {banned!r}"


def test_auto_source_embeds_no_harness_trigger_strings():
    # REQ-05: the payload stays harness-neutral - no harness-specific trigger.
    for trigger in ("clearanddo", "claude --print", "--dangerously"):
        assert trigger not in _AUTO_SRC, f"auto.py must not embed trigger {trigger!r}"


def test_auto_source_does_not_touch_reseed_or_advance_gate():
    # REQ-02: auto is a separate surface; its *code* never calls into the
    # ask-first reseed or the advance gate (prose may still name them).
    code = _auto_code_only()
    for banned in ("reseed_text", "CONFIRMATION_DIRECTIVE", "advance_project"):
        assert banned not in code, f"auto.py code must not reference {banned!r}"


def test_auto_adds_no_persisted_auto_on_config_key(tmp_path):
    # REQ-01/D-10: the opt-in is the per-invocation command; no `auto: true`
    # (or equivalent) auto-on key is persisted to the project config.
    slug = _active_at(tmp_path, "brainstorm")
    auto.auto_text(tmp_path)
    cfg_text = config.config_path(tmp_path).read_text()
    assert "auto" not in cfg_text.lower()
