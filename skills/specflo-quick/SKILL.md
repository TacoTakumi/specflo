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

1. **Scout.** Run `specflo followup list`: an open follow-up that an earlier
   project left may be this change. If it is, name its ID in the Goal, and
   close it after the commit with
   `specflo followup close FU-NN --by <project> --note "<what was done>"`.
   `--by` records what closed it: this project, or the commit's SHA.
2. **Fill the brief through the CLI.** Never open `brief.md` in an editor.
   - `specflo section set brief "Goal" --stdin` - one or two sentences.
   - `specflo section set brief "Done when" --stdin` - exactly one list item,
     a check a command or test can prove. Name the tests the change touches
     (a test file, a test ID or a `-k` filter), not the whole suite.
3. **Do the work.** Keep to what the check needs.
4. **Record proof.** Run the check and record its output:
   `specflo section set brief "Proof" --stdin`. Proof is the command and what
   it printed, not a claim that it passed.
5. **Validate.** `specflo validate brief` must pass: a goal, exactly one check,
   and proof.
6. **Commit once.** Run the full suite once, before the commit: the command
   `specflo config get test_command` prints, or the project's usual full test
   command when it prints nothing. One commit for the whole change; stage only
   the files you touched, never `git add -A`.
7. **Complete.** First add a follow-up for each item still in Deferred:
   `specflo followup add "<title>" --do "<what to do>"`. Nothing reads the
   brief after completion. In a ladder run, skip this: the next level picks
   Deferred up. Then `specflo advance` completes the project.
   There is **no review round** at quick level: the check and its proof are
   the gate.
8. **Report in chat.** Show the user the goal, the check and the proof. Nothing
   waits for them; they read it after the work is done.

## When the work grows

If you find work the brief does not cover, **stop**. Do not add it yourself.

- Not needed for the goal (a flaky test, a gap in another command): record it
  with `specflo followup add "<title>" --do "<what to do>"` and finish the one
  check.
- Needed for the goal, but small and not now: write it to the brief's
  Deferred section (`specflo section set brief "Deferred" --stdin`) and
  finish the one check.
- Needs more than one check: offer the user `specflo level fast`. The move
  keeps the brief and seeds a short brainstorm, spec and plan from it.
- In a ladder run (the auto payload carries a "Ladder run:" line) there is no
  user to ask: put the work in Deferred, finish the one check, and let the
  ladder move up.

## Red flags

- A second item in "Done when".
- Proof that says "it works" with no command output.
- More than one commit, or `git add -A`.
- Starting a review round at quick level.
- Adding uncovered work without the user.
