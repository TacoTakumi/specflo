---
name: specflo-execute
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
6. **Readiness** — when `specflo task show` reports no actionable task (every
   active task done, coverage holding), run a **final whole-branch review in
   fresh context**. Don't wait for a clean `specflo validate execute`: the review
   gate lives *inside* that validator, so it keeps reporting the missing review
   **until the round is closed** — a failing validate here is the reminder to
   review, not a reason to hold off:
   - Open the round first: `specflo review start` mints `review-N.md` and prints
     its path. The round is the reviewer's artifact, not a note in the chat.
   - With subagents: dispatch a reviewer on the most capable model — it verifies
     the *diff*, not your report (spec compliance + code quality), and returns
     ready-to-merge / not. Hand it the diff alone; a previous round's findings
     are yours to act on, not the fresh reviewer's to inherit.
   - Without subagents: do **not** review inline (it defeats fresh eyes and burns
     context) — `specflo checkpoint`, then run the review in a fresh session.
   - Record the outcome: `specflo review done --verdict ready-to-merge |
     changes-requested | waived` (waived needs `--reason`; `--file <path>` ingests
     a reviewer's report as the round's body). On `changes-requested`, fix what
     the round names, then run another round — rounds are numbered, and the
     latest one is the one that counts.
   On ready-to-merge, **pause before completing — don't auto-complete**: the work
   is done and reviewed and the **checkpoint is saved** (the project's
   `checkpoint.md`), so this is a safe place to stop. `specflo advance` completes
   the project — the user's to call; **wait** for their go.

   **Auto-mode carve-out.** The boundary pauses above are the *manual* default.
   Under an opt-in `specflo auto` run, the auto-mode bootstrap's **boundary
   override** (marked `== specflo auto-mode bootstrap ==`) drives the loop into
   and through execute without pausing at boundaries; completion then follows the
   bootstrap's terminal-stop — the loop halts on the CLI's `Completed project`
   signal (or a guardrail/kill escalation) and never auto-completes, so
   `specflo advance` still remains the user's call. This carve-out applies only
   under that bootstrap; absent it, pause as above.

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

## Fan-out

The project's **execution mode** is a recorded fact: read it from
`specflo status` (the `Execution:` line, or the `execution` key of `--json`).
Under **linear** the loop above is unchanged — work one task at a time exactly
as written. Under **fan-out** you are the *orchestrator*: the loop above is what
each *subagent* does for its task, and you drive the frontier:

1. **Frontier.** `specflo task list --json` — the tasks with `ready: true` are
   dispatchable now; the CLI has already removed file conflicts with in-progress
   work and pools with no free slot. The `pools` map shows every pool's size and
   its current holders.
2. **Dispatch.** Per ready task: `specflo task start T-NN`, then spawn
   **one subagent** with the full `specflo task show T-NN` brief and, for each
   pool in its `Needs`, the concrete **pool member** it is assigned (which GPU,
   which venv, which port). Subagents **never run `git` or `specflo`** — plan
   state and commits are yours — and edit only the task's **Files** plus any
   **new test files** they create.
3. **Close.** When a subagent returns, **re-run the Verify** step yourself; its
   report is not evidence. On pass: stage the task's files **by path** (never
   `git add -A`), commit **one task per commit**, then `specflo task done T-NN`.
   On fail: send the failure back (or start a fresh subagent with the same brief
   plus the failure) — never mark it done.
4. **Long tasks** (downloads, benches, anything measured in minutes) run in the
   **background**; keep dispatching the rest of the frontier and act on the
   completion notification. Never predict a pending agent's result.
5. **The `user` pool.** A task needing `user` is **never delegated**: it runs in
   the main session with the user, and holds the one `user` slot while it does.
6. **Recovery.** A slot held by a **dead** or abandoned agent is released with
   `specflo task reopen T-NN` (→ pending): the task returns to the frontier and
   its files and pool slots free.

Re-read the frontier after every `task done` / `task reopen` — the ready set
changes as slots and files free. Milestone boundaries still surface their Exit
checklist; the working-ahead note is suppressed under fan-out because lanes
crossing milestones is the expected shape.

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
- [ ] `specflo validate execute` exits 0. It stays red
      **until the round is closed**, so expect it to pass only once the review
      below is recorded — not before it.
- [ ] A fresh-context final whole-branch review ran under `specflo review start`
      and was recorded with `specflo review done --verdict …`; the latest round
      is ready-to-merge (or a reasoned waived).
- [ ] Then, and only then, surface the checkpoint-saved phase-end beat and leave `specflo advance` (project completion) to the user.
