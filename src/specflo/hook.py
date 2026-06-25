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

import json
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


# The SessionStart sources the reseed fires on (D-05): a true context wipe and a
# fresh session in a project. `compact`/`resume` are excluded (context retained).
RESEED_MATCHER = "startup|clear"
RESEED_COMMAND = "specflo hook reseed"


def settings_snippet() -> dict:
    """The ``.claude/settings.json`` fragment wiring the reseed into SessionStart.

    One SessionStart entry whose ``matcher`` fires on exactly ``startup`` and
    ``clear`` and whose command invokes ``specflo hook reseed``.
    """
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": RESEED_MATCHER,
                    "hooks": [{"type": "command", "command": RESEED_COMMAND}],
                }
            ]
        }
    }


def install_hook(root: Path) -> Path:
    """Merge the reseed SessionStart entry into ``root/.claude/settings.json``.

    Creates the file (and ``.claude/``) if absent, preserves all existing content,
    and is idempotent — re-running adds no duplicate when the reseed hook is
    already wired (matched on ``matcher`` + ``command``). Returns the settings path.
    """
    settings_path = root / ".claude" / "settings.json"
    settings: dict = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text() or "{}")
        except json.JSONDecodeError:
            settings = {}
    session_start = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    already_wired = any(
        entry.get("matcher") == RESEED_MATCHER
        and any(h.get("command") == RESEED_COMMAND for h in entry.get("hooks", []))
        for entry in session_start
    )
    if not already_wired:
        session_start.append(settings_snippet()["hooks"]["SessionStart"][0])
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return settings_path
