---
name: specflo-quick
description: Use when the active specflo project is at quick level (`specflo status` shows the quick level), or the user asks for a quick, small change through specflo ("just fix it", "quick one", "small tweak"). Works one brief.md with a goal, one check and proof, then completes the project. Do NOT use for fast or full level projects (use the specflo-brainstorm/spec/plan/execute skills), or for work that needs more than one check.
---

# quick

Drive a quick-level specflo project from its empty `brief.md` to a completed
project: a goal, exactly one "done when" check, the work, and the proof that the
check passes. The `specflo` CLI does the document I/O and the gate; this skill
carries the judgement: keep the work to one check, and stop when it grows.

## When to use

- `specflo status` shows `Level: quick` (phase is always `execute`).
- The user wants one small change: a typo, a one-line fix, a small tweak that
  one check can prove.

## When NOT to use

- The project is at fast or full level. Use the phase skills.
- The change needs more than one check, or a design choice the user should
  make. That is fast level: offer `specflo level fast`.

## Choosing the level

Before `specflo new`, propose a level and let the user confirm it:

- one goal and one check: `--level quick`
- 3 to 7 tasks: `--level fast`
- more: full (the default)

## Process

1. **Fill the brief through the CLI.** Never open `brief.md` in an editor.
   - `specflo section set brief "Goal" --stdin` - one or two sentences.
   - `specflo section set brief "Done when" --stdin` - exactly one list item,
     a check a command or test can prove.
2. **Do the work.** Keep to what the check needs.
3. **Record proof.** Run the check and record its output:
   `specflo section set brief "Proof" --stdin`. Proof is the command and what
   it printed, not a claim that it passed.
4. **Validate.** `specflo validate brief` must pass: a goal, exactly one check,
   and proof.
5. **Commit once.** One commit for the whole change; stage only the files you
   touched, never `git add -A`.
6. **Complete.** `specflo advance` completes the project. There is **no review
   round** at quick level: the check and its proof are the gate.
7. **Report in chat.** Show the user the goal, the check and the proof. Nothing
   waits for them; they read it after the work is done.

## When the work grows

If you find work the brief does not cover, **stop**. Do not add it yourself.

- Small and separate: write it to the brief's Deferred section
  (`specflo section set brief "Deferred" --stdin`) and finish the one check.
- Needs more than one check: offer the user `specflo level fast`. The move
  keeps the brief and seeds a short brainstorm, spec and plan from it.

## Red flags

- A second item in "Done when".
- Proof that says "it works" with no command output.
- More than one commit, or `git add -A`.
- Starting a review round at quick level.
- Adding uncovered work without the user.
