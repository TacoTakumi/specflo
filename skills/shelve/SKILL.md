---
name: shelve
description: Use when the user signals they want to set the active specflo project aside ("we're done with this", "drop this", "not worth it", "shelve it", "park this for now") — map that to `specflo shelve` — or to pick a paused project back up ("let's pick that back up", "resume that", "un-shelve it") — map that to `specflo resume`. The user need not name the command. Do NOT use to complete a finished project (that's `specflo advance`), or for a momentary in-session pause that needs no state change.
---

# shelve

Map a user's natural-language **stop** / **resume** intent onto specflo's
`shelve` / `resume` commands, so a project can be paused with its phase intact
and picked back up later — without the user having to name the command. The CLI
does the state work; this skill carries the intent recognition and the
conversation around it.

## When to use

- **Stop-intent** — the user signals they want to set the active project aside:
  "we're done with this", "drop this", "not worth it", "shelve it", "park this
  for now", "let's stop here". → `specflo shelve`.
- **Resume-intent** — the user wants to pick a shelved project back up: "let's
  pick that back up", "resume that", "un-shelve it", "back to <project>".
  → `specflo resume`.

## When NOT to use

- The project is **finished** (all work done and verified) — that is completion
  via `specflo advance`, not shelving. `specflo shelve` refuses a complete
  project.
- A momentary pause within one session that needs no persisted state change.
- The user explicitly names a different command — follow what they said.

## Process

### Shelve (stop-intent)
1. Confirm the target: the **active** project by default; a name targets another
   (`specflo shelve <name>`).
2. Capture an optional **reason** from what the user said — the "why we're
   stopping" (e.g. "not worth it", "blocked on the vendor") — and pass it as
   `--reason "…"`. A bare `specflo shelve` with no reason is valid.
3. Run `specflo shelve [<name>] [--reason "…"]`.
4. Tell the user it is shelved (the phase is preserved) and that
   `specflo resume` picks it back up.

Shelving sets `status` to `shelved`, **leaves the phase untouched**, and keeps
the `active_project` pointer where it is (you can still `switch` away). Re-shelving
updates the reason.

### Resume (resume-intent)
1. Identify the shelved project: the active one when it is shelved, or a name
   (`specflo resume <name>`).
2. Run `specflo resume [<name>]`.
3. The project returns to `status: active` **at the same phase** it was paused
   at, becomes the active project, and its reason is cleared. Pick the work back
   up from the checkpoint (`specflo checkpoint`).

`specflo resume` refuses a project that is not shelved — resume is the only
un-shelve verb.

## Notes

- `specflo guide` lists both `shelve` and `resume`; `specflo status`,
  `specflo list`, and `checkpoint.md` mark a shelved project and show its reason.
- Shelve/resume never change the phase — that orthogonality is what lets resume
  drop you back exactly where the work paused.

## Verification

- After shelving: `specflo status` shows the project marked `(shelved)` (with the
  reason when one was given), and `specflo list` shows the `⏸ shelved` marker.
- After resuming: `specflo status` shows it active again **at the preserved
  phase**, and it is the active project.
