---
name: auto
description: Use when the user wants an unattended specflo run — "auto mode", "autopilot", "run it on its own", "keep going without me", "don't stop to ask" — map that to `specflo auto`. It is an explicit, per-invocation opt-in that drives the pipeline across phase boundaries without the ask-first pause. Do NOT use for the normal attended pipeline (use the brainstorm/spec/plan/execute skills), or when the user wants to approve each boundary.
---

# auto

Turn a user's **unattended-run** intent into a `specflo auto` invocation, then
follow the payload it emits. `specflo auto` is the explicit, per-invocation opt-in
that runs the specflo pipeline (`brainstorm → spec → plan → execute`) from the
current phase toward completion **without** the ask-first confirmation pause. The
CLI does all the heavy lifting — it emits a self-contained payload carrying the
full autonomy policy, the guardrail stop-conditions, the verbatim checkpoint, and
the next step; this skill only recognises the intent, runs the command, and hands
the emitted directives back to the loop. **It reimplements none of that logic** —
the emitted bootstrap is the source of truth, not this file.

## When to use

- The user opts into an unattended run: "auto mode", "autopilot", "run it on its
  own", "keep going without stopping to ask", "don't pause at each phase".
  → `specflo auto`.

## When NOT to use

- The normal **attended** pipeline, where the user approves each phase boundary —
  use the `brainstorm` / `spec` / `plan` / `execute` skills, which pause and wait.
- The user wants to review or approve before crossing a boundary — that is the
  default; do not switch to auto for them.

## Process

1. **Run the command.** `specflo auto` (add `--autonomy …`, `--max-passes …` as
   the user asks; see below). specflo only *prints* a payload — it drives no loop,
   spawns no nested agent, and never clears context.
2. **Follow the emitted payload, verbatim.** It has three parts: the **auto-mode
   bootstrap** (marked `== specflo auto-mode bootstrap ==`) carrying the autonomy
   policy and every guardrail stop-condition; the **verbatim checkpoint**
   (read-first files, next action, any milestone beat); and the generated
   **next-step block** (marked `== specflo next step ==`) naming the current phase
   and its immediate next action. Obey the bootstrap's directives as written — do
   not paraphrase, second-guess, or relax them.
3. **Do the phase work under the bootstrap.** The next-step block points at the
   phase skill (`brainstorm` / `spec` / `plan` / `execute`) for the current phase —
   carry the phase work with it, but under the bootstrap's **boundary override**:
   once a phase validates, advance and keep going instead of pausing.
4. **The outer harness owns the loop, not specflo.** The seamless
   clear-context-and-continue trigger is the harness's job (e.g. a SessionStart
   hook / clearanddo / pi). Each continuing pass re-runs `specflo auto` — the
   bootstrap self-propagates, so the run stays in auto mode across a context clear
   rather than reverting to ask-first.
5. **Stop when the payload says stop.** When `specflo auto` emits an escalation,
   the kill-switch halt, or the completion directive (rather than a bootstrap),
   the run is over — hand off to the human; do not start another pass.

## Flags (surface only — semantics live in the CLI)

Pass these through as the user requests; for their exact behaviour and defaults,
defer to `specflo auto --help` and the emitted bootstrap — this skill does not
restate them:

- `--autonomy {safe,autonomous,yolo}` — how far the run goes unattended (default
  `safe`), with a matching config default.
- `--max-passes N` — the runaway pass cap; on reaching it the run escalates to the
  human instead of continuing.
- `--off` / `--on` — set or clear the durable auto-off kill switch for the active
  project.

## Notes

- `specflo auto` is **strictly additive**: the default attended `hook reseed` /
  `specflo checkpoint` pause is unchanged. Auto is a separate surface the user
  opts into per invocation — there is no persisted "auto on" default.
- specflo **owns the payload, never the trigger**: it emits harness-neutral plain
  text and leaves the clear-and-continue to whatever harness is driving.

## Verification

- `specflo auto` at the active project prints the three-part payload (the
  `== specflo auto-mode bootstrap ==` and `== specflo next step ==` markers, with
  the verbatim checkpoint between them) and exits 0.
- On a complete project, or when a guardrail trips, it prints a stop/escalation
  directive (no bootstrap) instead — the signal to hand off.
