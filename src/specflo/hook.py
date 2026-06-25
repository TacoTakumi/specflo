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

# Leads the reseed payload so the agent surfaces state and asks rather than
# auto-running the checkpoint's "Do next" (D-04). Source-neutral so it reads
# naturally for `startup`, `clear`, and `resume` alike.
CONFIRMATION_DIRECTIVE = (
    "You are resuming a specflo project. Do NOT begin work yet — the user may "
    "want to do something else, or not continue at all. Present the checkpoint "
    "below to the user and ask whether they want to continue, do something "
    "else, or stop, then wait for their answer."
)


def _active_project(cwd: Path):
    """``(root, project)`` for the active project found from ``cwd``, or ``None``.

    Shared resolver for the reseed entrypoints. May raise on a corrupt project;
    callers run it inside their own never-errors guard.
    """
    root = config.find_root(cwd)
    if root is None:
        return None
    cfg = config.load_config(root)
    if cfg.active_project is None:
        return None
    return root, projects.load_project(root, cfg, cfg.active_project)


def reseed_text(cwd: Path | None = None) -> str:
    """Return the reseed payload for the active project found from ``cwd``.

    The payload is :data:`CONFIRMATION_DIRECTIVE` followed by the verbatim
    ``specflo checkpoint`` render (single source of truth). Resolves the specflo
    root and active project from ``cwd`` (defaulting to the current directory).

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
        root, project = found
        body = checkpoint.render_checkpoint(checkpoint.build_checkpoint(root, project))
        return f"{CONFIRMATION_DIRECTIVE}\n\n{body}"
    except Exception:
        return ""


def _user_nudge(project) -> str:
    """The user-visible one-liner: which project is active and what to type.

    A SessionStart hook can re-ground the *agent* (via injected context) but
    cannot make it take a turn — so this is surfaced to the *human* to tell them
    the concrete thing to type to kick the reseed off. Harness-neutral wording.
    """
    return (
        f'specflo: active project "{project.slug}" (phase: {project.phase}). '
        "I won't pick up on my own — type `continue` and I'll surface the "
        "checkpoint and resume from there, or tell me what you'd like to do."
    )


def claude_session_start_output(cwd: Path | None = None) -> str:
    """Claude Code ``SessionStart`` JSON for the active project found from ``cwd``.

    Wraps the portable :func:`reseed_text` payload as ``additionalContext`` (for
    the agent) and adds a user-visible ``systemMessage`` (:func:`_user_nudge`)
    telling the human what to type. This is the only Claude-specific *shape* —
    the payload itself stays portable for other harnesses.

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
        _root, project = found
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
            "systemMessage": _user_nudge(project),
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
