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

from . import checkpoint, config, projects, status
from .continuation import (
    COMPLETE_DIRECTIVE,
    CONFIRMATION_DIRECTIVE,
    DIRECT_DIRECTIVE,
    SHELVED_DIRECTIVE,
)
from .projects import COMPLETE_STATUS, SHELVED_STATUS

# The four directives are re-exported, not defined here: `continuation.py` is the
# single producer of payload prose (pi-extension REQ-21), so this module selects
# a directive and assembles the payload but holds no copy of the wording.
__all__ = [
    "COMPLETE_DIRECTIVE",
    "CONFIRMATION_DIRECTIVE",
    "DIRECT_DIRECTIVE",
    "SHELVED_DIRECTIVE",
    "claude_session_start_output",
    "install_hook",
    "reseed_text",
    "settings_snippet",
]


def _active_project(cwd: Path):
    """``(root, cfg, project)`` for the active project found from ``cwd``, or ``None``.

    Shared resolver for the reseed entrypoints. May raise on a corrupt project;
    callers run it inside their own never-errors guard.
    """
    root = config.find_root(cwd)
    if root is None:
        return None
    cfg = config.load_config(root)
    if cfg.active_project is None:
        return None
    return root, cfg, projects.load_project(root, cfg, cfg.active_project)


def reseed_text(cwd: Path | None = None, *, direct: bool = False) -> str:
    """Return the reseed payload for the active project found from ``cwd``.

    The payload is a leading directive followed by the verbatim
    ``specflo checkpoint`` render (single source of truth). The directive is
    :data:`CONFIRMATION_DIRECTIVE` for a project still in flight,
    :data:`COMPLETE_DIRECTIVE` once it is complete (nothing to resume — offer a
    new project instead), or :data:`SHELVED_DIRECTIVE` when it is shelved (paused
    — offer resume or a new project). Resolves the specflo root and active
    project from ``cwd`` (defaulting to the current directory).

    With ``direct=True`` an in-flight project leads with :data:`DIRECT_DIRECTIVE`
    instead: an imperative "carry out the next step now" with no confirmation
    gate, for a caller that cleared context on purpose and has already answered
    "do you want to continue" (REQ-18). The flag changes nothing else — same body,
    same assembly — and it does **not** override the complete or shelved
    directives, since neither state has a next step to carry out.

    Returns ``""`` and never raises when there is nothing to emit (no specflo
    root, no active project, or an unreadable project) — even resolving the
    current directory happens inside the guard, so the session-start hook that
    calls it can be wired unconditionally and cannot break startup.
    """
    try:
        if cwd is None:
            cwd = Path.cwd()
        found = _active_project(cwd)
        if found is None:
            return ""
        root, _cfg, project = found
        body = checkpoint.render_checkpoint(
            checkpoint.build_checkpoint(root, project, cfg=_cfg)
        )
        if project.status == COMPLETE_STATUS:
            directive = COMPLETE_DIRECTIVE
        elif project.status == SHELVED_STATUS:
            directive = SHELVED_DIRECTIVE
        elif direct:
            directive = DIRECT_DIRECTIVE
        else:
            directive = CONFIRMATION_DIRECTIVE
        return f"{directive}\n\n{body}"
    except Exception:
        return ""


def _user_message(root: Path, cfg, project) -> str:
    """The user-visible session-start message: the ``specflo status`` block + a prompt.

    A SessionStart hook can re-ground the *agent* (via injected context) but
    cannot make it take a turn — so this is surfaced to the *human* at startup.
    It leads with the verbatim ``specflo status`` render (so "what the user sees"
    *is* status) and closes with the concrete next move: ``continue`` to resume a
    project still in flight, ``specflo resume``/``specflo new`` when it is shelved,
    or ``specflo new`` once it is complete (nothing to resume). Harness-neutral
    wording.
    """
    status_block = status.render_status(root, status.build_status(root, cfg, project))
    if project.status == COMPLETE_STATUS:
        prompt = (
            "This project is complete. Would you like to start a new project? "
            "(`specflo new`) - or tell me what you'd like to do."
        )
    elif project.status == SHELVED_STATUS:
        prompt = (
            "This project is shelved. Tell me to resume it (`specflo resume`), "
            "start a new project (`specflo new`), or what you'd like to do instead."
        )
    else:
        prompt = (
            "I won't pick up on my own - type `continue` and I'll surface the "
            "checkpoint and resume from there, or tell me what you'd like to do."
        )
    return f"{status_block}\n\n{prompt}"


def claude_session_start_output(cwd: Path | None = None) -> str:
    """Claude Code ``SessionStart`` JSON for the active project found from ``cwd``.

    Wraps the portable :func:`reseed_text` payload as ``additionalContext`` (for
    the agent) and adds a user-visible ``systemMessage`` (:func:`_user_message`)
    showing ``specflo status`` plus what to do next. This is the only
    Claude-specific *shape* — the payload itself stays portable for other
    harnesses.

    Returns ``""`` and never raises when there is nothing to emit, so the hook
    can be wired unconditionally and cannot break session start.
    """
    try:
        if cwd is None:
            cwd = Path.cwd()
        context = reseed_text(cwd)
        if not context:
            return ""
        found = _active_project(cwd)
        if found is None:
            return ""
        root, cfg, project = found
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
            "systemMessage": _user_message(root, cfg, project),
        }
        return json.dumps(payload)
    except Exception:
        return ""


# The SessionStart sources the reseed fires on: a true context wipe (`clear`), a
# fresh session (`startup`), and a resumed session (`resume`) so "came back"
# surfaces the checkpoint too. `compact` is excluded (the digest is retained).
RESEED_MATCHER = "startup|clear|resume"
# The installed hook emits Claude's SessionStart JSON (agent context + a visible
# user nudge); the bare `specflo hook reseed` stays portable plain text.
RESEED_COMMAND = "specflo hook reseed --format claude"
# Any reseed command, old or new, is matched on this prefix so install migrates
# a previously-wired entry in place instead of duplicating it.
_RESEED_COMMAND_PREFIX = "specflo hook reseed"


def settings_snippet() -> dict:
    """The ``.claude/settings.json`` fragment wiring the reseed into SessionStart.

    One SessionStart entry whose ``matcher`` fires on ``startup``, ``clear``, and
    ``resume`` and whose command invokes :data:`RESEED_COMMAND`.
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


def _is_reseed_entry(entry) -> bool:
    """Whether ``entry`` is one of our reseed hooks (any matcher/command variant)."""
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("hooks"), list)
        and any(
            isinstance(h, dict)
            and isinstance(h.get("command"), str)
            and h["command"].startswith(_RESEED_COMMAND_PREFIX)
            for h in entry["hooks"]
        )
    )


def install_hook(root: Path) -> Path:
    """Merge the reseed SessionStart entry into ``root/.claude/settings.json``.

    Creates the file (and ``.claude/``) if absent and preserves all unrelated
    content. Idempotent and self-migrating: any prior reseed entry (any older
    matcher/command form) is dropped and replaced with the current wiring, so
    re-running never duplicates and an out-of-date entry is rewired in place.
    Returns the settings path.
    """
    settings_path = root / ".claude" / "settings.json"
    settings: dict = {}
    if settings_path.is_file():
        try:
            loaded = json.loads(settings_path.read_text() or "{}")
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            settings = loaded
    # Coerce away unexpected shapes (a hand-edited settings.json could have a
    # non-object `hooks` or a non-list `SessionStart`) so the merge can't raise.
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = settings["hooks"] = {}
    session_start = hooks.setdefault("SessionStart", [])
    if not isinstance(session_start, list):
        session_start = hooks["SessionStart"] = []
    # Drop any prior reseed entry (migrate, don't duplicate), then append ours.
    session_start[:] = [e for e in session_start if not _is_reseed_entry(e)]
    session_start.append(settings_snippet()["hooks"]["SessionStart"][0])
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return settings_path
