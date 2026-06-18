---
name: spec
description: Use at a specflo project's spec phase, when turning a validated brainstorm into a structured, testable spec before any plan or code. Triggers include "write the spec", "let's spec this out", or `specflo status` showing the spec phase. Do NOT use for trivial fixes or before a brainstorm has been validated.
---

# Spec (specflo)

## Overview

Turn a validated `brainstorm.md` into a **validated `spec.md`** for the active
specflo project: an objective, numbered **testable `REQ-NN` requirements** (each
with a pass/fail acceptance criterion, traced to the `D-NN` decision it derives
from), explicit in/out boundaries, and open questions — then hand off toward the
plan phase. The `specflo` CLI does the artifact I/O; you carry the synthesis,
judgment, and discipline.

## When to use / When NOT

**Use** at the spec phase (the second of `brainstorm → spec → plan → execute`),
once the brainstorm is validated. The spec is a **synthesis of the brainstorm,
not a new interview** — read `brainstorm.md` and write requirements; do not
re-litigate settled decisions.

**Do NOT use** for trivial fixes, or before a brainstorm exists and passes
`specflo validate brainstorm`. If the brainstorm is incomplete, finish it first.

## HARD-GATE

Do NOT write code, scaffold anything, or take any implementation action until the
spec is validated (`specflo validate spec` passes) and the user has explicitly
approved. The discipline here is the **rigor of the artifact** — testable
requirements with pass/fail acceptance — not merely "don't code yet".

## Process

1. **Preflight.** Confirm an active project at the spec phase (`specflo status`).
   Run `specflo spec start` to create or locate `spec.md` (the command prints its
   path — never build the path yourself). Read the project's `brainstorm.md`:
   its Decisions (`D-NN`), Research, Current understanding, and Out of scope.
2. **Draft requirements — reframe vague → testable.** For each thing the system
   must do, write a falsifiable requirement. Reframe fuzzy wants:
   `"make it fast"` → `"the landscape scan returns within 5s on a warm cache"`,
   then ask "are these the right targets?". A good requirement is **✓** "API
   responds in < 200ms at p95"; a bad one is **✗** "the system should be fast".
3. **Give every requirement a pass/fail acceptance criterion.** No subjective
   criteria — write how a verifier confirms it. Trace it to the decision it comes
   from with `--from D-NN` where applicable.
4. **Capture inline.** The moment a requirement is settled, record it:
   `specflo requirement add --text "…" --acceptance "…" [--from D-NN]`
   (add `--supersedes REQ-NN` when it replaces an earlier one). Don't batch —
   capture as they happen. Keep the prose sections (Objective, Boundaries In/Out,
   Open questions, Canonical refs) current by editing `spec.md` directly.
5. **Hold the scope boundary.** Fill **Boundaries** — In scope and Out of scope —
   carrying the brainstorm's Out of scope / Deferred forward. Both lists must be
   non-empty.
6. **No stale specifics.** The spec is behavioral and durable: no file paths or
   code snippets that churn. One exception — a small prototype snippet that
   encodes a decision more precisely than prose can (a schema or type shape).
7. **Self-review.** Re-read the spec with fresh eyes: placeholder scan; internal
   consistency (do requirements contradict?); two-way ambiguity (pick one reading
   and make it explicit); scope (one plan, or should this decompose?).
8. **Gate + validate.** Ask the user an explicit "ready?". On yes, run
   `specflo validate spec`; fix any reported gaps inline and re-run until it
   passes.
9. **Hand off.** Tell the user the spec is complete and the plan phase is next:
   the plan synthesizes from `spec.md` and cites `REQ-NN` — it must not
   re-interview. Phase movement is `specflo advance`'s job — do not change the
   phase yourself.

## Anti-sycophancy

Take a position on every requirement and state what evidence would change it.
Avoid filler validation: "That's a great requirement," "That could work," "There
are many ways to think about this." Challenge the strongest version of the
user's intent, not a strawman. If a requirement isn't testable, say so and
reframe it.

## Common rationalizations

| Rationalization | Reality |
|---|---|
| "The brainstorm is enough." | The brainstorm captures *decisions*, not *testable requirements*. The spec adds acceptance criteria a verifier can check. |
| "I'll write the acceptance after I code it." | That's a test report, not a specification. Decide pass/fail *before* building. |
| "This requirement is obviously testable." | If you can't state the pass/fail check in one line, it isn't. Write the line. |
| "I'll re-ask the user to be safe." | Re-interviewing is not synthesis. Read `brainstorm.md`; only ask about genuine gaps. |
| "Boundaries are obvious." | Unwritten scope is where plans balloon. Write In and Out explicitly. |

## Red flags (stop and correct)

- A requirement has no pass/fail acceptance criterion.
- You wrote a requirement into chat prose instead of `specflo requirement add`.
- You re-interviewed the user on something the brainstorm already decided.
- You put file paths or churn-prone code snippets into `spec.md`.
- You moved toward the plan without an explicit user "ready?" and a passing
  `specflo validate spec`.
- You built or scaffolded something during the spec phase.

## Verification checklist

- [ ] `specflo spec start` was run; `spec.md` exists for the active project.
- [ ] Every requirement is in the Requirements section via `specflo requirement add`.
- [ ] Every requirement has a pass/fail **Acceptance** criterion.
- [ ] **Boundaries** — In scope and Out of scope — are both filled in.
- [ ] **Open questions** is present (may say "none").
- [ ] `specflo validate spec` passes.
- [ ] The user explicitly approved readiness before handoff.
- [ ] No code or scaffolding was produced.
