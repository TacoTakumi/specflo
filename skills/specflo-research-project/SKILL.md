---
name: specflo-research-project
description: Use when the active specflo project is at research level (`specflo status` shows the research level, or the checkpoint says to start with `specflo research recap`), or the user wants to research an idea, a market or a product before or instead of building ("start a research project on X", "let's find out whether X is worth building", "pick up the research"). Runs one research session, which opens with the recap, asks questions one at a time, records each source, finding, question, term and decision with the CLI as it lands, and ends with a session close. Do NOT use for a build project at quick, fast, full or harden level (use the specflo-quick, phase or specflo-harden skills), or for a one-off fact check inside a brainstorm (that is the specflo-research skill).
---

# research project

Drive a session of a research project: a long-running, multi-session study of
an idea or a product, held in a research document (destination, usual user,
decisions, log) and four ledgers (sources S-NN, questions Q-NN, findings
RF-NN, glossary terms). The usual user is often a domain expert, not a
developer. The `specflo` CLI keeps the document, the ledgers, the log and the
grounding check; this skill carries the conversation: draw the user's
knowledge out, ground every finding, record everything as it lands, and hand
the next session a clear start.

## When to use

- `specflo status` shows `Level: research`, or the checkpoint says to start
  with `specflo research recap`.
- The user wants to learn whether an idea holds, what a market or a field
  looks like, or what a product should be, before (or instead of) building.
- A later session of a research project that other sessions have already
  worked.

## When NOT to use

- A build project. Quick, fast, full and harden level projects have their own
  skills; a research project that turns into a build goes through
  `specflo level fast|full` or `specflo research spinoff` first.
- A one-off fact check inside a brainstorm. That is the specflo-research
  skill, dispatched by specflo-brainstorm.
- A report written in one sitting from a web search alone. A research
  project is the user's knowledge and sources, worked over sessions.

## Process

1. **Start every session with `specflo research recap`.** It prints the
   destination and why it was last changed, the usual user, the last
   session's summary and next steps, the open questions grouped by who
   answers them, the ledger counts, any spin-off that rests on a replaced
   entry, and the outgoing-search rule. Read it before you say anything.
   Never open `research.md` or a ledger in an editor; the CLI is the only
   way in.
2. **On the first session only, record who this is for and where it goes.**
   Ask the user to describe the usual user of the research in their own
   words, then record it as given:
   `specflo research user set --text <text> | --file <path>`. Then ask what
   the research is for and record it:
   `specflo research destination set build|inform|learn|unknown --reason <text>`.
   Unknown is a fine first answer. Do not ask who is at the keyboard in later
   sessions; the recap shows it. Change either only when the user says it
   has changed; the log keeps the old one.
3. **Set the tone early.** Early in the session tell the user they can always
   ask for your recommendation on any question. Then follow these rules for
   the whole session:
   - Ask one question at a time. Wait for the answer before the next one.
   - Questions that draw out the user's knowledge (what they know, have seen,
     have tried, who they serve) carry no suggested answer: a guess would
     steer an expert. Questions about a choice (which direction, which of two
     readings, what to do next) carry a recommended answer and the reason,
     so silence still moves the work.
   - Use plain language when the usual user is not a developer: no file
     names, framework names, CLI output or code in what you say to them. Say
     what a thing does, not what it is called. The commands below are yours
     to run, not theirs to read.
4. **Work the next question.** Take it from the recap's open questions or
   the last session's next steps. Pick the one whose answer changes the most;
   ask the user when two seem equal.
5. **Record each source, finding, question, term and decision with the CLI as
   it lands.** Do not batch them to the end of the session; a session that
   stops early then loses nothing.
   - A document the user gives you:
     `specflo research ingest <file>` (markdown and text; PDF and DOCX with
     the `specflo[docs]` extra). An interview, a web page or a repository:
     `specflo research source add --kind interview|web|repo --title <text> (--file <path> | --stdin) [--url <url>]`.
     Record what the user tells you as an interview source in their words,
     not yours.
   - A finding:
     `specflo research finding add --text <text> --kind fact|inference --status supported|contradicted|unsure [--cites S-NN[:pN]]... [--supersedes RF-NN]`.
     A fact needs a citation; a page number when the source has pages. What
     you conclude from facts is an inference, and says so. Mark a finding
     unsure rather than round it up to supported.
   - A question the session raises but cannot answer:
     `specflo research question add --text <text> --by user|developer [--option <text>]... [--default <text>] [--why <text>] [--supersedes Q-NN]`.
     When it is answered:
     `specflo research question resolve Q-NN --resolution <text> [--by <name>]`.
   - A word the field uses, or one the user wants avoided:
     `specflo research term set --term <term> --definition <text> [--avoid <word>]...`.
     Then use the user's words, not your synonyms.
   - A decision the user makes about the research or the idea:
     `specflo decision add --text <text> --rationale <text>`, in words the
     user would recognise.
   - Entries are never deleted. When one is wrong or has been overtaken,
     replace it with `--supersedes`; the old one stays in the ledger and the
     recap shows any spin-off that rested on it.
   - `specflo research list sources|questions|findings|terms [--open] [--json]`
     shows a ledger when you need to check one.
6. **Send the technical questions to a developer, broad and few.** Mark a
   question for a developer when it is technical (how a thing could be built,
   what it would cost, whether a system allows it):
   `--by developer`, with a `--default` you would pick without them and a
   `--why` that says what the answer changes, and keep developer questions
   broad: one question about the shape of a thing beats five about its parts,
   and the developer answers in one sitting. Do not ask the usual user a
   developer's question in a developer's words. `specflo research ask-dev`
   prints the open developer questions as email text the user can send; it
   sends nothing.
7. **Keep the user's material out of outgoing searches.** Never put names,
   private figures or text from the project's sources in an outgoing web
   search; search for the public topic, the field and the published work
   instead. The CLI prints one egress suggestion at the first source
   (`specflo egress local|no-train|open`); whether or not the user pins a
   class, the outgoing-search rule holds whatever the egress class. A web
   source you find is recorded like any other, with its URL.
8. **Check the grounding before the close.** `specflo research lint` reports
   each finding whose numbers, dates or quoted strings are not in its cited
   sources. It changes nothing and blocks nothing; fix a finding it names with
   a superseding entry or a better citation, or leave it unsure and say so in
   the summary.
9. **Offer a lesson to the wiki only when there is one and the user agrees.**
   A research project keeps its own knowledge in its folder. Save a lesson to
   a wiki only when one is installed and the user agrees, through that wiki's
   own tool. The CLI never writes to a wiki.
10. **End every session with `specflo research session close --summary <text> --next <text> [--present <who>]`.**
    The summary says what the session settled and what it found; the next
    steps name the question to open next, so the next recap starts the next
    session. Give `--present` when someone other than the usual user was at
    the keyboard. Then stop; the close is the end of the session.
11. **Know the ways out, and leave them to the user.** When the user decides
    to build, `specflo research spinoff <name> --level quick|fast|full --use <ID>[,<ID>]...`
    starts a build project seeded with the named findings, questions and
    sources while the research goes on, or `specflo level fast|full` turns
    the research project itself into a brainstorm at that level. When the
    research has done its job, `specflo advance` completes it once a
    destination, a usual user and one session are recorded. Each of these is
    the user's call: name it, and wait.

## Rationalizations

| Rationalization | Reality |
|---|---|
| "I remember where we were; skip the recap." | The recap is the one place the last session's next steps, the open questions and the rule set are read together. Run it first, every time. |
| "A suggested answer saves the expert time." | On a knowledge question it steers the expert toward your guess. Suggest on choices, not on knowledge. |
| "I will record the findings at the close." | A session that stops early loses them, and the user cannot see what landed. Record each entry as it lands. |
| "That figure is not in the source, but it is close." | Close is contradicted or unsure, never supported. Cite what the source says or mark the finding unsure. |
| "The search needs the client's name to be useful." | Names, private figures and source text never go in an outgoing search, whatever the egress class. Search for the public topic. |
| "The developer can sort the details; I will list them all." | Developer questions are broad and few, with a default and a why, so one email settles them. |
| "This lesson belongs in the wiki." | Only when a wiki is installed and the user agrees. The research project's folder is where its knowledge lives. |

## Red flags

- A session that did not begin with `specflo research recap`, or that asked
  who is at the keyboard after the first session.
- Two questions in one message, or a knowledge question with a guess
  attached.
- A choice question with no recommended answer.
- Code, file names, framework names or CLI output in what a non-developer
  user reads.
- A finding of kind fact with no citation, or a figure a source does not
  carry marked supported.
- A source, finding, question, term or decision held in chat instead of
  recorded with the CLI.
- An entry deleted or edited by hand instead of superseded.
- A name, a private figure or source text in an outgoing web search.
- A wiki save without an installed wiki and the user's agreement.
- A session that ended without `specflo research session close`, or a
  spin-off, level change or `specflo advance` the user did not call.
