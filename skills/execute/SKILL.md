---
name: execute
description: Use at a specflo project's execute phase, when turning a validated plan.md into committed, verified code one task at a time. Triggers include "execute the plan", "start building", "work the next task", or `specflo status` showing the execute phase. Do NOT use before a plan has been validated, or for trivial fixes outside the pipeline.
---

# execute

Drive a validated `plan.md` to a complete project: work each `T-NN` task as a
thin vertical slice — implement, test, verify, review, commit — then complete the
project behind a reconcile gate and a fresh-context final review. The CLI owns
state and the gates (`specflo task show`, `task start|done|block`, `validate
execute`, `advance`); this skill carries the loop, the implementation judgement,
and the discipline.

## When to use

- At the **execute phase**, after `specflo validate plan` passed and `specflo
  advance` moved the project into execute.
- **Synthesize, don't re-plan** — the plan decided *what* and *in what order*;
  execute *implements* it. If the plan is wrong, supersede a task — don't replan
  in place.

## When NOT to use

- Before a plan is validated, or with no active project at the execute phase.
- For trivial one-off fixes that don't warrant the pipeline.

## HARD-GATE

Work the plan as written. If a task can't be done as specified, **supersede it**
(`specflo task add --supersedes T-NN …`) or recommend a phase split / a
superseding requirement — never silently mutate a task or drift off its
`Implements: REQ-NN`. *"I'll just change the plan"* is replanning, not executing.

## Process

1. **Preflight** — confirm an active project at the execute phase. Run `specflo
   task show` to get the next actionable task's brief (its acceptance, verify
   step, cited `REQ-NN` sections, and Global constraints). Read **only** the
   brief — not the whole spec (keep context low).
2. **Per-task loop** — for the task from `task show`:
   1. `specflo task start T-NN` (→ in_progress).
   2. Implement the thinnest slice that satisfies the acceptance criterion. For
      behavior-adding tasks, write the failing test first (**RED**), then the
      minimal code (**GREEN**) — the task's Verify names the test.
   3. Run the task's **Verify** step; capture the passing evidence.
   4. **Self-review**: the verify step actually ran and passed; the diff matches
      the acceptance criterion; only the task's files changed; no scope creep.
   5. **Commit** one atomic commit for the task — stage only the files it
      touched, never `git add -A`, so any point is a clean `git revert`.
   6. `specflo task done T-NN` (it refuses unless the task is in_progress).
   7. Next: `specflo task show` again.
3. **Checkpoint freely** — it is safe to stop between any two tasks: run `specflo
   checkpoint` and clear context; resume drops you back at the next actionable
   task. Long executions should clear context between tasks to stay sharp.
4. **Failure / blocked** — when a task's verify fails or you hit a wall, run a
   bounded diagnose loop (reproduce → hypothesise → fix → re-verify). If
   unresolved, `specflo task block T-NN --reason "…"` and escalate to the human —
   never power through. If the *plan* is wrong, supersede the task.
5. **Stop on irreversibility** — destructive migrations, data deletion, secret
   handling, posting/outbound actions, or "anything you can't undo with `git
   revert`": stop and checkpoint with the human before proceeding.
6. **Readiness** — when `specflo task show` reports no actionable task and
   `specflo validate execute` is clean (all tasks done, coverage holds), run a
   **final whole-branch review in fresh context**:
   - With subagents: dispatch a reviewer on the most capable model — it verifies
     the *diff*, not your report (spec compliance + code quality), and returns
     ready-to-merge / not.
   - Without subagents: do **not** review inline (it defeats fresh eyes and burns
     context) — `specflo checkpoint`, then run the review in a fresh session.
   On ready-to-merge, **pause before completing — don't auto-complete**: the work
   is done and reviewed and the **checkpoint is saved** (the project's
   `checkpoint.md`), so this is a safe place to stop. `specflo advance` completes
   the project — the user's to call; **wait** for their go.

## Milestones

When the plan groups tasks into **milestones**, the CLI surfaces two soft signals
in the `specflo task show` brief (and in `status` / `checkpoint`) — honour them,
don't reimplement them:

- **Boundary verify beat.** When a milestone's last task completes, the brief
  surfaces the just-completed milestone's **Exit checklist** with a soft,
  user-gated *proceed* prompt. It never blocks the loop: pause, verify the Exit
  items with the human, then continue. There is deliberately no "milestone done"
  verb — the beat is derived, not marked.
- **Working-ahead label.** With no ready task left in the current milestone,
  `task show` steers to the earliest ready later-milestone task, labelled
  *working ahead* — fine to take, just know you've crossed the boundary.

Inspect milestones read-only with `specflo milestone list` / `specflo milestone
show M-NN`; milestone state is derived from task progress, never hand-edited.

## `task done` is earned

A task is done when its Verify step ran and passed and the diff matches the
acceptance — not when it "looks done." Check the diff, not your own report; a
stated rationale never downgrades a real gap.

## Anti-sycophancy

Do not open with "All done!", "Looks perfect", or similar. State what's
unverified or incomplete plainly. A green checkbox over an unrun verify step is
worse than an honest "blocked."

## Rationalizations

| Rationalization | Reality |
|---|---|
| "It basically works." | Completion needs fresh verification evidence, not a vibe — run the Verify step. |
| "The test will slow me down." | A behavior-adding task without a failing-then-passing test isn't done. |
| "I'll just tweak the plan as I go." | That's replanning — supersede the task instead (**HARD-GATE**). |
| "I'll mark it done and circle back." | `task done` means done and verified; don't flip ahead of the evidence. |
| "I'll review my own work inline." | The final review must be fresh context — subagent or a fresh session. |

## Red flags

- Calling `task done` without having run the task's Verify step.
- A commit that stages unrelated files (`git add -A`) instead of the task's own.
- Editing a task in place instead of superseding it when the plan is wrong.
- Advancing to completion without a fresh-context whole-branch review.
- Loading the whole spec instead of the `task show` brief.

## Verification

Before completing the project:

- [ ] Every active task is `done` and each earned its own atomic commit.
- [ ] `specflo validate execute` exits 0 (coverage holds; all tasks done).
- [ ] A fresh-context final whole-branch review returned ready-to-merge.
- [ ] Then, and only then, surface the checkpoint-saved phase-end beat and leave `specflo advance` (project completion) to the user.
