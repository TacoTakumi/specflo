---
name: specflo-plan
description: Use at a specflo project's plan phase, when turning a validated spec into a dependency-ordered, testable task plan before any code. Triggers include "write the plan", "break this into tasks", or `specflo status` showing the plan phase. Do NOT use for trivial fixes or before a spec has been validated.
---

# plan

Drive a validated `spec.md` to a validated `plan.md` for the active project: a
synthesized approach, dependency-ordered `T-NN` tasks (each with behavioral
acceptance, a verification step, and `Implements: REQ-NN` traceability), and a
clean handoff toward execution. The CLI does the structured work (`specflo plan start`, `specflo task add`, `specflo validate plan`, `specflo advance`); this
skill carries the synthesis, decomposition judgement, and discipline.

## When to use

- At the **plan phase**, after a validated `spec.md`.
- **Synthesize, don't re-spec** — the spec captured *what* to build; the plan
  captures *in what order, and verified how*.

## When NOT to use

- Before a spec has been validated (`specflo validate spec` must pass).
- For trivial one-off fixes that don't warrant the pipeline.

## HARD-GATE

No implementation code until the plan is validated (`specflo validate plan`
passes) and the user approves. *"I'll just write the code"* → that's execution,
not planning.

## Process

1. **Preflight** — confirm an active project at the plan phase; run `specflo plan start` to create/locate `plan.md`; **read `spec.md`** (the `REQ-NN`s + their
   acceptance + boundaries) and the brainstorm's architecture decisions as input.
   Treat `spec.md` as read-only.
2. **Decompose into vertical slices** — each task is a thin, end-to-end,
   independently testable/reviewable deliverable. Reject horizontal layers.
   Split a task when it would take >2 h, needs >3 acceptance bullets, touches ≥2
   independent subsystems, or has "and" in its title.
3. **Shape each task** — a behavioral pass/fail **acceptance** criterion, a
   **verification** command/step, and `Implements: REQ-NN` (repeatable). Structure
   behavior-adding tasks as RED→GREEN (write the failing test first). Declare
   `Depends on` for ordering.
4. **Capture inline** — run `specflo task add` with `--text … --acceptance … --verify … --from REQ-NN [--from REQ-NN …] [--depends-on T-NN …]` the moment each task
   lands (un-batched). Keep Approach / Global constraints / Open questions /
   Canonical refs updated as prose.
5. **Scope-reduction guard** — the plan delivers what each `REQ-NN`/`D-NN`
   requires; never silently degrade to "v1 / simplified / for now / a stub." On
   genuine overflow, recommend a phase split or a superseding requirement. Clear
   `specflo validate plan` scope-reduction warnings before advancing.
6. **Coverage by construction** — every active `REQ-NN` is implemented by ≥1 task,
   and every task cites ≥1 live `REQ-NN`. Aim for this so `specflo validate plan` passes first try.
7. **Self-review** — coverage both ways, no placeholders, dependencies ordered and
   acyclic, ≤~5 files per task, every task has acceptance + verification.
8. **Readiness + phase boundary** — get an explicit user **"ready?"**, then
   `specflo validate plan` → fix issues + address warnings → re-validate. Then
   **pause at the boundary, don't auto-advance**: the plan is complete and
   validated, the **checkpoint is saved** (the project's `checkpoint.md`; resume any
   time with `specflo checkpoint`), so this is a **safe place to clear context**.
   `specflo advance` (moves `plan → execute`) is the user's to call — **wait** for
   their go; don't start executing tasks yourself.

   **Auto-mode carve-out.** The pause-and-wait above is the *manual* default.
   Under an opt-in `specflo auto` run, the auto-mode bootstrap's **boundary
   override** (marked `== specflo auto-mode bootstrap ==`) supersedes it — once
   the phase validates, advance across `brainstorm → spec → plan → execute` on
   your own without pausing here. This carve-out applies only under that
   bootstrap; absent it, pause as above.

## Milestones

Group the plan's tasks into ordered **milestones** — user-verifiable slices —
when it spans more than a couple. The CLI owns every milestone mechanic
(document-order sequence, rollup, the current milestone, the boundary beat); this
skill only directs *authoring*, so reference the commands rather than restating
that logic:

- Author each milestone with an **Exit checklist** (what proves the slice works):
  `specflo milestone add --text "Auth works" --exit "login" --exit "logout"`.
- Assign **every** task to a milestone — `--milestone M-NN` on `specflo task add`,
  or `specflo task set-milestone T-NN M-NN` after the fact. Once milestones
  exist, `specflo validate plan` flags any unassigned task, requires milestone
  dependencies to point backward, and checks the per-milestone `REQ-NN` union
  covers the spec.
- Milestones are **optional**: a plan with zero milestones stays dormant and
  behaves exactly as today — add them only when they earn their keep.

## Anti-sycophancy

Do not open with "Great plan!", "You're absolutely right", or similar. State the
plan's gaps plainly. A plan that hides a coverage hole to seem agreeable is worse
than no plan.

## Rationalizations

| Rationalization | Reality |
|---|---|
| "The spec is enough." | The spec says *what*; the plan says *in what order, verified how*. |
| "I'll just write the code." | That's execution, not planning — **HARD-GATE**. |
| "This task is obvious; skip the acceptance." | Every task carries a pass/fail criterion by construction. |
| "Ship a simplified version for now." | Deliver what the requirement needs, or split — don't silently reduce scope. |
| "Every task is one big slice." | Prefer many thin vertical slices over few thick ones. |

## Red flags

- A task with no `Implements: REQ-NN`, or an active requirement no task covers.
- Acceptance criteria that are not pass/fail, or a task with no verification step.
- Horizontal layer tasks ("all the models", "all the endpoints") instead of
  end-to-end slices.
- Dependency edges that form a cycle, or that reference a non-existent task.

## Verification

Before declaring the plan ready:

- [ ] `specflo validate plan` exits 0 (bidirectional coverage holds; every task has acceptance + verification; dependencies resolve and are acyclic).
- [ ] Any `specflo validate plan` warnings are addressed or justified.
- [ ] The user has explicitly approved the plan.
- [ ] Then, and only then, surface the checkpoint-saved phase-end beat and leave `specflo advance` to the user.
