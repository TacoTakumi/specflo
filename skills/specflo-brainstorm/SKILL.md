---
name: specflo-brainstorm
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

## Levels

specflo has three levels of ceremony. Before `specflo new`, propose one from
the request and have the user confirm it:

- **quick** - one goal and one check (`specflo new <name> --level quick`): one
  brief, proof, no review. Worked with the `specflo-quick` skill, not this one.
- **fast** - 3 to 7 tasks (`--level fast`): a short brainstorm, spec and plan
  that you write yourself, with one approval before execute.
- **full** - more than that (the default): this skill's interview, and an
  approval at every phase.

A project moves up with `specflo level fast|full`, never down.

**After a move up to full.** `specflo level full` on a fast project goes back
to the brainstorm and lists the decisions made at fast level. You made those
without an interview, so review each one with the user, one at a time: confirm
it, or record the replacement with `specflo decision add --supersedes D-NN`.
Then continue the interview from there; do not start the brainstorm again.

## Process

1. **Preflight.** Confirm an active project (`specflo status`). A new project's
   `brainstorm.md` is already **scaffolded by `specflo new`**, so here
   `specflo brainstorm start` just **locates** it (creating it only if missing —
   e.g. a pre-existing project) and prints its locator (`<project>/brainstorm`)
   — never build a path yourself. If resuming, read it with
   `specflo doc show brainstorm` to load prior decisions; do not re-litigate
   them.
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
   official client), not just what you assumed. Run `specflo followup list` as
   well: an open follow-up that an earlier project left may be this work or a
   piece of it. Name the ones this project takes up in the brainstorm, so the
   execute phase closes them when the work lands.
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
   the prose sections (Current understanding, Research, Out of scope / Deferred,
   Open questions, Canonical refs) current with
   `specflo section set brainstorm "<section>" --file <path>` (or `--stdin`):
   it replaces that one section's body and leaves every decision untouched.
   Never open `brainstorm.md` in an editor — the CLI is the only way in, and
   the Decisions section is refused there (it belongs to `decision add`).
7. **Hold the scope boundary.** Scope is fixed: clarify HOW, not WHETHER to add
   new capabilities. Park scope-creep under **Out of scope / Deferred** — don't
   lose it, don't act on it.
8. **Check readiness.** You are ready when you can predict the user's reaction to
   the next three questions you'd ask, and your confidence is high. State a
   confidence number. If after several rounds you still can't converge, say so —
   something foundational is missing; step back.
9. **Gate + validate.** Ask the user an explicit "ready?". On yes, run
   `specflo validate brainstorm`; fix any reported gaps inline (`section set`
   the prose sections / add missing decisions) and re-run until it passes.
10. **Hand off — pause at the phase boundary.** Surface the end of the phase as
    one clear beat, and **do not auto-advance**: the brainstorm is complete and
    validated, the **checkpoint is saved** (the project's `checkpoint.md`; resume
    any time with `specflo checkpoint`), so this is a **safe place to clear
    context**. The spec phase is next (it synthesizes from `brainstorm.md` and must
    not re-interview). Then **wait** — `specflo advance` is the next move, but it's
    the user's to call; don't change the phase yourself or start the spec.

    **Auto-mode carve-out.** The pause-and-wait above is the *manual* default.
    Under an opt-in `specflo auto` run, the auto-mode bootstrap's **boundary
    override** (marked `== specflo auto-mode bootstrap ==`) supersedes it — once
    the phase validates, advance across `brainstorm → spec → plan → execute` on
    your own without pausing here. This carve-out applies only under that
    bootstrap; absent it, pause as above.

## Researching (the woven research seam)

Research is done by a **research subagent**, not by you inline — that keeps raw
search noise out of this conversation. Dispatch a **general-purpose** subagent and
instruct it to follow the `skills/specflo-research` skill (it runs with least-privilege,
read/research-only tools and owns wiki search + save-back) — do **not** pass
`subagent_type: research`; research is a *skill*, not an agent type, and that call
fails. Hand it one research question and fold the **digest** it returns into the
artifact:

- **Findings / Surprises** → the Research section via `section set` (and revise
  **Current understanding** the same way).
- **Sources** → **Canonical refs**.
- A fact that grounds a decision → cite it in that decision's `--rationale`.

Two triggers:
- **Landscape scan** — once, early (step 3), mandatory. The safety net for the
  things you didn't know to ask about. Announce it; don't gate it.
- **Opportunistic check** — before a fact-dependent decision (step 6). Run it
  inline-fast and report the result; no per-check permission prompt.

**Portability / degraded mode:** the dispatch is the only harness-specific part.
Where subagents aren't available, run the `skills/specflo-research` process inline instead
(accept the added noise). Where the wiki is absent, research proceeds web-only.

## Requester mode

The daemon's opening prompt tells you which seat you serve. When it says the
seat is the **requester**, you are talking to someone who owns the problem but
is not a developer, over the project page's chat. The process above still
runs, with these changes, until a developer takes over:

- **Plain language.** No code, file names, framework names, or CLI output in
  what you say. Say what a thing does, not what it is called.
- **What and why, not how.** Ask what the requester needs and why it matters;
  never ask them to choose an implementation. Where a technical choice has
  to be made, pick the reasonable default yourself and note it as a
  developer's call under **Open questions**.
- **One scan, early, presented plainly.** The landscape scan (step 3) still
  runs once, up front: dispatch it as usual, fold the digest into
  **## Research** and **Canonical refs** as usual, and tell the requester what
  it found in plain words, since what already exists steers their decisions
  too. Opportunistic checks per decision stay.
- **Decisions in plain words.** Record each decision with
  `specflo decision add` as it lands, written so the requester would recognise
  it: the outcome they asked for and the reason they gave. Technical
  consequences go in the rationale, not the decision text.
- **Open the gate when the requester is done.** When the requester says they
  are done, or has nothing more to add, do not ask "ready?" and do not
  validate or advance: run `specflo gate open developer --note "<open points>"`
  with a one-line note listing what is still open for the developer (a
  decision you defaulted, an unverified fact, a question they could not
  answer). Then tell the requester a developer will pick it up from here. The
  daemon's inbox shows the gate to every developer.
- **Flip back on takeover.** When a message from the daemon says the
  **developer** seat has taken the gate, leave requester mode: continue the
  brainstorm by the normal process above, in the same session, with the scan
  and every decision so far inherited. Do not re-interview what the requester
  settled; pick up the open points from the note.
- **Relay the take on the developer's word.** A developer who is with you in
  the pane may say they are taking the project rather than taking it from the
  page. On their word, run `specflo gate take --by developer`: only your
  identity may pass `--by`, and it records the developer as the taker. Never
  take a gate on your own account.

## Fast level

When `specflo status` shows `Level: fast`, this skill runs short:

- **You make the decisions.** At most 3, recorded with `specflo decision add`.
  For each, weigh 2 or 3 approaches, pick one, and put the approaches weighed
  and why you chose that one in the `--rationale`.
- **Do not interview the user,** and **do not pause** at the phase boundary:
  once the phase validates, run `specflo advance` and go on, from brainstorm to
  spec to plan.
- **Deferred work** goes in the brainstorm's Out of scope / Deferred section.
- **Caps:** 3 decisions and 7 tasks. A cap only warns: finish the work already
  recorded, add no more, and put new work in the brainstorm's Out of scope /
  Deferred section. Do not move the project up; the user chose fast.
- **One approval, before execute.** When the plan validates, stop. Show the
  user `specflo doc show brief` and list every choice you made that they have
  not seen: each decision, and any requirement or task beyond what they asked
  for. Run `specflo advance` into execute only after they approve.

Write a short Current understanding and skip the research scan unless a decision rests on a fact you cannot check in the code.

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
- In requester mode: you asked the requester a how question, named a file or
  a framework, asked "ready?" or validated instead of opening the gate, or
  took the gate without a developer's word.

## Verification checklist

- [ ] `brainstorm.md` exists for the active project (scaffolded by `specflo new`; `specflo brainstorm start` locates it).
- [ ] Every decision reached is in the Decisions section via `specflo decision add`.
- [ ] Every prose section was written with `specflo section set`, never by editing the file.
- [ ] **Out of scope / Deferred** is filled in (not just the scaffold comment).
- [ ] **Open questions** is present (may say "none").
- [ ] `specflo validate brainstorm` passes.
- [ ] The user explicitly approved readiness before handoff.
- [ ] At hand-off, the checkpoint-saved phase-end beat was surfaced and `specflo advance` was left to the user.
- [ ] No code or scaffolding was produced.
- [ ] The landscape scan ran and its digest was folded into the artifact.
- [ ] Fact-dependent decisions cite a source (or the uncertainty is in Open questions).
