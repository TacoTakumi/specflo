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

1. **Preflight.** Confirm an active project (`specflo status`). A new project's
   `brainstorm.md` is already **scaffolded by `specflo new`**, so here
   `specflo brainstorm start` just **locates** it (creating it only if missing —
   e.g. a pre-existing project) and prints its path — never build the path
   yourself. If resuming, read the existing file to load prior decisions; do not
   re-litigate them.
2. **Decompose-first.** If the request spans multiple independent subsystems, say
   so now and split it; brainstorm one piece at a time. Don't refine details of
   something that should be decomposed.
3. **Scout (code + landscape).** Read relevant code, recent commits, and the
   existing artifact; if a question can be answered by reading the codebase, read
   it — don't ask. Then **dispatch the research landscape scan** (see *Researching*
   below): announce it ("scanning for existing clients/SDKs/framework state…"),
   hand the research subagent the rough goal, and fold its digest into **Current
   understanding**, **## Research**, and **Canonical refs** *before* setting the
   agenda — so the gray areas reflect what already exists (an existing SDK, an
   official client), not just what you assumed.
4. **Set the agenda (gray areas).** Surface 3–4 phase-specific ambiguities —
   decisions that could go multiple ways and would change the result. Let the
   user pick which to dig into. Avoid generic labels.
5. **Ask one question at a time, with a recommended answer.** Each question
   carries your guess and reasoning, so silence still moves the design forward
   and your assumptions stay falsifiable. Annotate options with their codebase
   consequence (reuses X / needs new Y). Highest-risk decisions first.
6. **Capture decisions inline.** The moment a decision lands, record it:
   `specflo decision add --text "…" --rationale "…"` (add `--supersedes D-NN`
   when it replaces an earlier one). Don't batch — capture as they happen. Before
   recording a decision that rests on a **checkable fact** (a library's maturity,
   an API's capability, a version), run an **opportunistic research check** (see
   *Researching*) and cite the source in the rationale; if it can't be verified,
   record the uncertainty under **Open questions** rather than asserting it. Keep
   the prose sections (Current understanding, ## Research, Out of scope / Deferred,
   Open questions, Canonical refs) current by editing `brainstorm.md` directly.
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
10. **Hand off — pause at the phase boundary.** Surface the end of the phase as
    one clear beat, and **do not auto-advance**: the brainstorm is complete and
    validated, the **checkpoint is saved** (the project's `checkpoint.md`; resume
    any time with `specflo checkpoint`), so this is a **safe place to clear
    context**. The spec phase is next (it synthesizes from `brainstorm.md` and must
    not re-interview). Then **wait** — `specflo advance` is the next move, but it's
    the user's to call; don't change the phase yourself or start the spec.

## Researching (the woven research seam)

Research is done by a **research subagent**, not by you inline — that keeps raw
search noise out of this conversation. Dispatch a subagent that follows the
`skills/research` skill (it runs with least-privilege, read/research-only tools
and owns wiki search + save-back). Hand it one research question and fold the
**digest** it returns into the artifact:

- **Findings / Surprises** → `## Research` (and revise **Current understanding**).
- **Sources** → **Canonical refs**.
- A fact that grounds a decision → cite it in that decision's `--rationale`.

Two triggers:
- **Landscape scan** — once, early (step 3), mandatory. The safety net for the
  things you didn't know to ask about. Announce it; don't gate it.
- **Opportunistic check** — before a fact-dependent decision (step 6). Run it
  inline-fast and report the result; no per-check permission prompt.

**Portability / degraded mode:** the dispatch is the only harness-specific part.
Where subagents aren't available, run the `skills/research` process inline instead
(accept the added noise). Where the wiki is absent, research proceeds web-only.

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
- You set the gray-areas agenda without running the landscape scan.
- You recorded a fact-dependent decision without grounding it (or noting it
  unverified under Open questions).

## Verification checklist

- [ ] `brainstorm.md` exists for the active project (scaffolded by `specflo new`; `specflo brainstorm start` locates it).
- [ ] Every decision reached is in the Decisions section via `specflo decision add`.
- [ ] **Out of scope / Deferred** is filled in (not just the scaffold comment).
- [ ] **Open questions** is present (may say "none").
- [ ] `specflo validate brainstorm` passes.
- [ ] The user explicitly approved readiness before handoff.
- [ ] At hand-off, the checkpoint-saved phase-end beat was surfaced and `specflo advance` was left to the user.
- [ ] No code or scaffolding was produced.
- [ ] The landscape scan ran and its digest was folded into the artifact.
- [ ] Fact-dependent decisions cite a source (or the uncertainty is in Open questions).
