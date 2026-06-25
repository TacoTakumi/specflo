"""The `specflo hook` integration — reseed a freshly-started session.

A Claude Code ``SessionStart`` hook cannot make the agent remember what to do
after a ``/clear``; the continuation must come from outside the conversation.
``reseed_text`` returns the clear-and-continue payload — a confirmation-gate
directive plus the verbatim ``specflo checkpoint`` render — for the active
project found from a working directory, which the hook injects as context so a
blank-slate agent reorients itself and *asks before resuming*.

Mirrors ``checkpoint.py``: a pure function over project state, no I/O of its own
beyond reading the artifacts the checkpoint already derives from.
"""

from __future__ import annotations

from pathlib import Path

from . import checkpoint, config, projects

# Leads the reseed payload so the freshly-cleared agent surfaces state and asks
# rather than auto-running the checkpoint's "Do next" (D-04). Source-agnostic so
# it reads naturally for both `clear` and `startup`.
CONFIRMATION_DIRECTIVE = (
    "You are resuming a specflo project in a fresh session. Do NOT begin work "
    "yet — the user may want to do something else, or not continue at all. "
    "Present the checkpoint below to the user and ask whether they want to "
    "continue, do something else, or stop, then wait for their answer."
)


def reseed_text(cwd: Path) -> str:
    """Return the reseed payload for the active project found from ``cwd``.

    The payload is :data:`CONFIRMATION_DIRECTIVE` followed by the verbatim
    ``specflo checkpoint`` render (single source of truth). Resolves the specflo
    root and active project from ``cwd``.

    Returns ``""`` and never raises when there is nothing to emit (no specflo
    root, no active project, or an unreadable project), so the session-start
    hook that calls it can be wired unconditionally and cannot break startup.
    """
    try:
        root = config.find_root(cwd)
        if root is None:
            return ""
        cfg = config.load_config(root)
        if cfg.active_project is None:
            return ""
        project = projects.load_project(root, cfg, cfg.active_project)
        body = checkpoint.render_checkpoint(checkpoint.build_checkpoint(root, project))
        return f"{CONFIRMATION_DIRECTIVE}\n\n{body}"
    except Exception:
        return ""
