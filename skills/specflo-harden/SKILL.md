---
name: specflo-harden
description: Use when the active specflo project is at harden level (`specflo status` shows the harden level), or the user asks to harden code that already exists through specflo ("harden this module", "shake the bugs out of X", "review and fix it until it is clean"). Works one brief that names the scope, runs fresh-context harden rounds whose findings become fix tasks, and stops on the user's say. Do NOT use for new work (use specflo-quick or the phase skills), or for the review at the end of a normal project (that is the specflo-execute skill).
---

# harden

Drive a harden-level specflo project: a brief that names what to harden, then
harden rounds in which a fresh-context reviewer reads that code and records
findings, a fix task for each finding that blocks, and a stop that the user
calls. The `specflo` CLI does the document I/O, the round and task state and
the gates; this skill carries the judgement: keep the scope honest, fix every
path to a defect, and leave the stop to the user.

## When to use

- `specflo status` shows `Level: harden` (the phase is always `execute`).
- The user wants code that already exists made sound: a module, a set of
  paths, or the whole repo, reviewed and fixed until review finds nothing new.

## When NOT to use

- New work. Hardening fixes the code that is there; a feature or a change is
  a quick, fast or full project.
- The review at the end of a normal project. That is the specflo-execute
  skill; its harden rounds review the branch, not a brief's Scope.
- An unattended run. Hardening is attended: the user stops it. `specflo auto`
  refuses a harden project, and `specflo level` never moves a project into or
  out of harden level.

## Process

1. **Start the project.** Propose harden level and let the user confirm it,
   then run `specflo new <name> --level harden` (add `--remote <daemon>` to
   hold it on a daemon). The project starts at execute with a brief to fill
   and an empty plan.
2. **Fill the brief through the CLI.** Never open `brief.md` in an editor.
   - `specflo section set brief "Scope" --stdin` - the paths to harden, one
     list item each, or the whole repo. Scope cannot be none.
   - `specflo section set brief "Focus" --stdin` - what the rounds look at
     hardest (error paths, input from outside, concurrency), or none.
   - `specflo section set brief "Stop when" --stdin` - when hardening stops,
     beyond an empty ledger and every fix task done, or none.
   Agree the Scope with the user: it is the boundary of every round. Then
   `specflo validate brief` must pass.
3. **Open a harden round.** `specflo review start --harden` opens it. It
   reviews the brief's Scope: a problem already present there is a finding,
   however old it is. A problem outside the Scope is not a finding.
4. **Hand the round to a fresh reviewer.** Give the reviewer the brief that
   `specflo review prompt` prints, so every round works to the same rules and
   sees the Scope and the Focus. With subagents: dispatch a fresh-context
   reviewer on the most capable model. Without subagents: do not review
   inline; run `specflo checkpoint`, then review in a fresh session. The
   reviewer records through the CLI:
   - `specflo review finding add --severity blocker|should-fix|nit --at <file>:<line>[-<line>] --text "..."`
     for each finding. A blocker or should-fix names its evidence in the text:
     a failing test, a command and its output, or the input that breaks it.
   - `specflo review finding check F-NN closed|open` for each earlier item
     the round lists.
   - A problem outside the Scope is a follow-up:
     `specflo followup add "<title>" --do "<what to do>"`.
5. **Close the round.** `specflo review done` closes it hardened and prints
   its new finds. Nits stay in the round.
6. **Fix each blocker and should-fix item with a fix task**, never the nits:
   `specflo task add --fixes F-NN --text "..." --acceptance "..." --verify "..."`.
   Every task in a harden project is a fix task. Its acceptance lists every
   path that reaches the defect, with a test for each; its verify names the
   pin test, red then green: it fails before the fix and passes after. Work
   each one through the execute loop: `specflo task start T-NN`, the failing
   test, the fix, the verify, one commit per task (stage only its files, never
   `git add -A`), then `specflo task done T-NN`. A path the fix missed keeps
   the item open.
7. **Open the next round.** With every fix committed, run
   `specflo review start --harden` again. The round checks each open item and
   reviews the whole Scope again. It refuses while an open item has no done
   fix task. Go on from step 4.
8. **Defer or reject only on the user's say.** Ask; do not pick for them.
   `specflo review finding defer F-NN --do "<what>"` files a follow-up and
   settles the item (refused for a project on a daemon);
   `specflo review finding reject F-NN --reason "<why>"` settles it as not a
   defect.
9. **Know when to stop.** There is no round budget and no task cap. After two
   harden rounds in a row with no new blocker or should-fix find, the
   next-step hint (`specflo status`) suggests the stop. Stopping is the user's
   call: tell them, and wait for their word. The brief's Stop when can ask for
   more. To go on, open another harden round.
10. **At the stop, run the whole test suite once**: the command
    `specflo config get test_command` prints, or the project's usual full test
    command when it prints nothing. The rounds run only the tests that
    reproduce a finding.
11. **Complete.** `specflo validate execute` names what is left. Completion
    needs a valid brief, a harden round closed hardened, no open round, every
    fix task done and an empty ledger: each blocker and should-fix item
    checked closed, deferred or rejected. An item still open at the stop needs
    its fix and a round that checks it closed (`specflo status` names the
    move), or the user's defer or reject. Then pause: `specflo advance`
    completes the project, and that is the user's call.

## Rationalizations

| Rationalization | Reality |
|---|---|
| "It is old code, so it is not a finding." | In a harden project a problem already present in the Scope is a finding, however old it is. |
| "The fix covers the case the reviewer named." | The acceptance lists every path to the defect, each with a test; a missed path keeps the item open. |
| "Two quiet rounds, so we are done." | The hint only suggests the stop. Stopping is the user's call. |
| "I can check my own fixes inline." | Each round needs a fresh-context reviewer: a subagent or a fresh session. |
| "This finding is not worth a fix." | Defer and reject are the user's say. Ask. |
| "I will fix the nits while I am here." | Nits stay in the round. Fix only blocker and should-fix items. |

## Red flags

- A blocker or should-fix finding with no evidence in its text.
- A fix task whose verify names no pin test, or a pin test that never failed.
- A fix for a nit, or a change outside the Scope.
- A defer, a reject or a stop that the user did not say.
- A round reviewed inline instead of by a fresh-context reviewer.
- More than one task in a commit, or `git add -A`.
- `specflo advance` without the user's go.
