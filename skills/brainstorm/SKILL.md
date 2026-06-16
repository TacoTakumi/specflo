---
name: brainstorm
description: Use at the start of a specflo project's brainstorm phase, when turning a fuzzy idea into a captured, validated understanding before any spec or code. Triggers include "let's figure out what we're building", "brainstorm X", or `specflo status` showing the brainstorm phase. Do NOT use for trivial fixes or when a sufficient written brief already exists.
---

# Brainstorm (specflo)

## Overview

Drive a fuzzy idea to a **validated `brainstorm.md`** for the active specflo
project: a synthesized understanding, an append-only decision record, an explicit
out-of-scope boundary, and open questions — then hand off toward the spec phase.
The `specflo` CLI does the artifact I/O; you carry the conversation, judgment,
and discipline.

## When to use / When NOT

**Use** at the brainstorm phase (the front of `brainstorm → spec → plan →
execute`), when the goal is still fuzzy and no design exists yet.

**Express path:** if a sufficient written brief/PRD already exists, skip the
interview — capture its decisions with `specflo decision add` and move on.

**Do NOT use** for trivial fixes (a typo, a one-line change) or when an approved
design already exists. Don't re-interview a settled understanding — synthesize.

## HARD-GATE

Do NOT write code, scaffold anything, or take any implementation action until the
brainstorm is validated (`specflo validate brainstorm` passes) and the user has
explicitly approved. This applies regardless of how simple the work looks.

## Process

1. **Preflight.** Confirm an active project (`specflo status`). Run
   `specflo brainstorm start` to create or locate `brainstorm.md` (the command
   prints its path — never build the path yourself). If resuming, read the
   existing file to load prior decisions; do not re-litigate them.
2. **Decompose-first.** If the request spans multiple independent subsystems, say
   so now and split it; brainstorm one piece at a time. Don't refine details of
   something that should be decomposed.
3. **Scout.** Read relevant code, recent commits, and the existing artifact. If a
   question can be answered by reading the codebase, read it — don't ask.
4. **Set the agenda (gray areas).** Surface 3–4 phase-specific ambiguities —
   decisions that could go multiple ways and would change the result. Let the
   user pick which to dig into. Avoid generic labels.
5. **Ask one question at a time, with a recommended answer.** Each question
   carries your guess and reasoning, so silence still moves the design forward
   and your assumptions stay falsifiable. Annotate options with their codebase
   consequence (reuses X / needs new Y). Highest-risk decisions first.
6. **Capture decisions inline.** The moment a decision lands, record it:
   `specflo decision add --text "…" --rationale "…"` (add `--supersedes D-NN`
   when it replaces an earlier one). Don't batch — capture as they happen. Keep
   the prose sections (Current understanding, Out of scope / Deferred, Open
   questions, Canonical refs) current by editing `brainstorm.md` directly.
7. **Hold the scope boundary.** Scope is fixed: clarify HOW, not WHETHER to add
   new capabilities. Park scope-creep under **Out of scope / Deferred** — don't
   lose it, don't act on it.
8. **Check readiness.** You are ready when you can predict the user's reaction to
   the next three questions you'd ask, and your confidence is high. State a
   confidence number. If after several rounds you still can't converge, say so —
   something foundational is missing; step back.
9. **Gate + validate.** Ask the user an explicit "ready?". On yes, run
   `specflo validate brainstorm`; fix any reported gaps inline (edit the prose
   sections / add missing decisions) and re-run until it passes.
10. **Hand off.** Tell the user the brainstorm is complete and the spec phase is
    next: the spec synthesizes from `brainstorm.md` and must not re-interview.
    Phase movement is `specflo advance`'s job (a later command) — do not change
    the phase yourself.

## Anti-sycophancy

Take a position on every answer and state what evidence would change it. Avoid
filler validation: "That's an interesting approach," "That could work," "You
might want to consider…," "There are many ways to think about this." Challenge
the strongest version of the user's idea, not a strawman.

## Common rationalizations

| Rationalization | Reality |
|---|---|
| "This is too simple to need a brainstorm." | Simple work is where unexamined assumptions cost most. Keep it short — but capture it. |
| "I'll figure out the details as I build." | Switching costs after code exists are ~10x. Decide now. |
| "They said 'whatever you think.'" | That's delegation, not a decision. Re-ask with two concrete options and a recommendation. |
| "I'll record all the decisions at the end." | End-of-session capture loses decisions and isn't resumable. Record each as it lands. |
| "It's faster to just start coding." | The HARD-GATE exists because skipped design is the expensive failure mode, not a shortcut. |

## Red flags (stop and correct)

- You asked more than one question in a single message.
- You wrote a decision into chat prose instead of `specflo decision add`.
- You're refining details of something that should have been decomposed.
- You moved toward the spec without an explicit user "ready?" and a passing
  `specflo validate brainstorm`.
- You built or scaffolded something during the brainstorm.

## Verification checklist

- [ ] `specflo brainstorm start` was run; `brainstorm.md` exists for the active project.
- [ ] Every decision reached is in the Decisions section via `specflo decision add`.
- [ ] **Out of scope / Deferred** is filled in (not just the scaffold comment).
- [ ] **Open questions** is present (may say "none").
- [ ] `specflo validate brainstorm` passes.
- [ ] The user explicitly approved readiness before handoff.
- [ ] No code or scaffolding was produced.
