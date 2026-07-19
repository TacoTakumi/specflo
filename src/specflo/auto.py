"""The `specflo auto` handoff payload - the auto-mode opt-in surface.

`specflo auto` is the explicit, per-invocation opt-in that starts or continues an
unattended run from the current phase toward project completion. Like ``hook.py``
it is pure derivation over project state: it *emits a payload* and never drives a
loop, spawns a nested agent, or clears context - the seamless clear-and-reseed
trigger is the outer harness's job (REQ-05).

Strictly additive (REQ-02): the ask-first reseed (``hook.reseed_text`` /
``CONFIRMATION_DIRECTIVE``) and the manual pipeline gates are untouched; this is a
separate surface. The opt-in is the per-invocation command - no persisted
per-project auto-on default is introduced (REQ-01).
"""

from __future__ import annotations

from pathlib import Path

from . import config, projects

# Fixed marker opening the auto-mode bootstrap section of the payload. Tests key
# on this structurally (never on verbatim wording), and later tasks grow the
# autonomy/guardrail directives beneath it without moving the marker.
BOOTSTRAP_MARKER = "== specflo auto-mode bootstrap =="

# Fixed clause markers within the bootstrap. Tests key on these structurally so
# later tasks can grow each clause's wording in place. BOUNDARY_OVERRIDE_MARKER
# labels the clause that supersedes the phase skills' pause gate (REQ-06).
BOUNDARY_OVERRIDE_MARKER = "Boundary override:"


def _boundary_override_clause() -> str:
    """The clause superseding the phase skills' boundary-pause HARD-GATE (REQ-06).

    In auto mode the brainstorm/spec/plan/execute skills' phase-boundary pause and
    wait-for-ready gate are overridden so the run flows across the pipeline on its
    own. The override lives only in this bootstrap; the manual reseed/checkpoint
    pause beat is untouched (REQ-02).
    """
    return (
        f"- {BOUNDARY_OVERRIDE_MARKER} in auto mode the phase-boundary pause and "
        "the wait-for-ready HARD-GATE of the brainstorm/spec/plan/execute skills "
        "are SUPERSEDED. Do not stop to ask at a phase boundary; once a phase "
        "validates, advance and keep going across "
        "brainstorm -> spec -> plan -> execute on your own. This override applies "
        "only under this auto bootstrap - the manual pipeline's pause is unchanged."
    )


def auto_bootstrap(phase: str) -> str:
    """Return the auto-mode bootstrap directive block for ``phase``.

    The bootstrap is the standing autonomy policy + guardrail stop-conditions the
    unattended run carries: an opt-in framing header followed by the directive
    clauses. Later tasks grow the fork-policy / autonomy / guardrail clauses
    beneath the ones here.
    """
    header = (
        f"{BOOTSTRAP_MARKER}\n"
        f"You are running in specflo auto mode at the '{phase}' phase: an "
        "explicit, per-invocation unattended run that continues the specflo "
        "pipeline from here toward project completion. specflo only emits this "
        "directive - it does not drive the loop or clear context for you."
    )
    clauses = [
        _boundary_override_clause(),
    ]
    return "\n".join([header, "", *clauses])


def _active_project(cwd: Path):
    """``(root, cfg, project)`` for the active project found from ``cwd``, or ``None``.

    Mirrors the resolver in ``hook.py``; kept local so ``auto`` stays an
    independent, additive surface. May raise on a corrupt project; callers run it
    inside their own never-errors guard.
    """
    root = config.find_root(cwd)
    if root is None:
        return None
    cfg = config.load_config(root)
    if cfg.active_project is None:
        return None
    return root, cfg, projects.load_project(root, cfg, cfg.active_project)


def auto_text(cwd: Path | None = None) -> str:
    """Return the auto handoff payload for the active project found from ``cwd``.

    For now the payload is the auto-mode bootstrap for the project's current
    phase; later tasks wrap it into the self-contained three-part reseed payload
    (bootstrap + verbatim checkpoint + generated next-step).

    Returns ``""`` and never raises when there is nothing to emit (no specflo
    root, no active project, or an unreadable project) - even resolving the
    current directory happens inside the guard, so `specflo auto` is safe to
    invoke at any phase and cannot break on a half-set-up tree.
    """
    try:
        if cwd is None:
            cwd = Path.cwd()
        found = _active_project(cwd)
        if found is None:
            return ""
        _root, _cfg, project = found
        return auto_bootstrap(project.phase)
    except Exception:
        return ""
