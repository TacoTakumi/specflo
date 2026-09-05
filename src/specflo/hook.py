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
import os
from pathlib import Path

from . import checkpoint, config, continuation, plan, projects, status
from .continuation import CONFIRMATION_DIRECTIVE, DIRECT_DIRECTIVE
from .projects import COMPLETE_STATUS, SHELVED_STATUS

# The two directives are re-exported, not defined here: `continuation.py` is the
# single producer of payload prose, so this module selects a directive and
# assembles the payload but holds no copy of the wording.
__all__ = [
    "CONFIRMATION_DIRECTIVE",
    "DIRECT_DIRECTIVE",
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


def _task_brief_text(root: Path, cfg, project) -> str | None:
    """The current task's brief, rendered — or ``None`` when there is none to give.

    Only the execute phase has a task to carry out, so the earlier phases return
    ``None`` without touching the plan (REQ-19). Every failure below that is
    swallowed: the brief is *enrichment*, and a plan that cannot produce one must
    still yield the directive and the checkpoint rather than a blank payload.
    """
    if project.phase != "execute":
        return None
    try:
        task_id = plan.current_task_id(root, cfg, project.slug)
        if task_id is None:
            return None
        return plan.render_task_brief(
            plan.task_brief(root, cfg, project.slug, task_id)
        ) or None
    except Exception:
        return None


def reseed_text(
    cwd: Path | None = None, *, direct: bool = False, directory_source: str | None = None
) -> str:
    """Return the reseed payload for the active project found from ``cwd``.

    The payload is a leading directive followed by the verbatim
    ``specflo checkpoint`` render (single source of truth). The directive is
    :data:`CONFIRMATION_DIRECTIVE` for a project still in flight. A complete or
    shelved active project has nothing to resume, so it emits nothing at all:
    the session starts silent. Resolves the specflo root and active project
    from ``cwd`` (defaulting to the current directory).

    With ``direct=True`` an in-flight project leads with :data:`DIRECT_DIRECTIVE`
    instead: an imperative "carry out the next step now" with no confirmation
    gate, for a caller that cleared context on purpose and has already answered
    "do you want to continue". The flag changes nothing else — same body, same
    assembly — and a complete or shelved project stays silent under it too,
    since neither state has a next step to carry out.

    ``directory_source`` is the CLI's record of which override put the process
    where it is (``"flag"`` for ``-C``, ``"env"`` for ``SPECFLO_DIRECTORY``,
    ``None`` for neither); it only decides how the leading override line
    credits the redirect (REQ-13).

    Returns ``""`` and never raises when there is nothing to emit (no specflo
    root, no active project, a complete or shelved one, or an unreadable
    project) — even resolving the
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
        if project.status in (COMPLETE_STATUS, SHELVED_STATUS):
            return ""
        body = checkpoint.render_checkpoint(
            checkpoint.build_checkpoint(root, project, cfg=_cfg)
        )
        brief = None
        if direct:
            directive = DIRECT_DIRECTIVE
            brief = _task_brief_text(root, _cfg, project)
        else:
            directive = CONFIRMATION_DIRECTIVE
        return _directory_override_line(root, directory_source) + continuation.build_reseed(
            directive, body, brief
        )
    except Exception:
        return ""


def _directory_override_line(root: Path, directory_source: str | None = None) -> str:
    """One leading line when ``SPECFLO_DIRECTORY`` is set in the environment.

    The env var is the silent case: a session that inherits it sees a payload
    that looks like the cwd repo's, so the line is emitted whenever the variable
    is set (a set-but-empty value counts as unset). A ``-C`` flag alone adds
    nothing - the caller typed it. When both are present the flag won the
    directory, so the line credits ``-C`` rather than the variable.
    """
    if not os.environ.get("SPECFLO_DIRECTORY"):
        return ""
    via = "-C" if directory_source == "flag" else "SPECFLO_DIRECTORY"
    return f"Directory override: specflo commands act on {root} (via {via}).\n"


def _user_message(root: Path, cfg, project) -> str:
    """The user-visible session-start message: the ``specflo status`` block + a prompt.

    A SessionStart hook can re-ground the *agent* (via injected context) but
    cannot make it take a turn — so this is surfaced to the *human* at startup.
    It leads with the verbatim ``specflo status`` render (so "what the user sees"
    *is* status) and closes with the concrete next move: ``continue`` to resume
    the project in flight. Only in-flight projects reach here; a complete or
    shelved one is silent upstream in :func:`reseed_text`. Harness-neutral
    wording.
    """
    status_block = status.render_status(root, status.build_status(root, cfg, project))
    prompt = (
        "I won't pick up on my own - type `continue` and I'll surface the "
        "checkpoint and resume from there, or tell me what you'd like to do."
    )
    return f"{status_block}\n\n{prompt}"


def claude_session_start_output(
    cwd: Path | None = None, *, directory_source: str | None = None
) -> str:
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
        context = reseed_text(cwd, directory_source=directory_source)
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
