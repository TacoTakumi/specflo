from specflo import checkpoint, config, hook, projects


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
