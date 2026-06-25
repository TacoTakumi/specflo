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
